#!/usr/bin/env python3
"""Small, locally hosted 3CX bridge for PRO installations.

The PBX database is only read. Profiles and global queue state use authenticated
SIP dial codes; individual queue state uses 3CX's local QueueAgent API.
"""

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo


CONFIG_PATH = Path(os.environ.get("SIMPLE_API_3CX_CONFIG", "/var/lib/simple-api-3cx/config.json"))
PARIS = ZoneInfo("Europe/Paris")
WRITE_LOCK = threading.Lock()
PROFILE_CODES = {
    "available": ("*30", "Available"),
    "away": ("*31", "Away"),
    "dnd": ("*32", "Out of office"),
    "custom1": ("*33", "Custom 1"),
    "custom2": ("*34", "Custom 2"),
}


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def number(value):
    if not re.fullmatch(r"[0-9]{1,8}", value or ""):
        raise ApiError(400, "Numéro de poste ou de file invalide")
    return value


def load_config():
    with CONFIG_PATH.open(encoding="utf-8") as stream:
        return json.load(stream)


def save_config(config):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".config-", dir=CONFIG_PATH.parent)
    try:
        os.fchmod(fd, 0o600)
        if CONFIG_PATH.exists():
            previous = CONFIG_PATH.stat()
            os.fchown(fd, previous.st_uid, previous.st_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temp_name, CONFIG_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def query_json(sql):
    env = os.environ.copy()
    env["PGOPTIONS"] = "-c default_transaction_read_only=on -c statement_timeout=10000"
    result = subprocess.run(
        ["psql", "-X", "-q", "-t", "-A", "-d", "database_single", "-c", sql],
        capture_output=True, text=True, timeout=13, env=env,
    )
    if result.returncode:
        raise ApiError(502, "Lecture du PBX indisponible")
    raw = result.stdout.strip()
    return json.loads(raw) if raw else None


def user_sql(filter_extension=None):
    where = f"where d.value='{number(filter_extension)}'" if filter_extension else ""
    return f"""
      select coalesce(json_agg(row_to_json(x) order by x.extension), '[]'::json) from (
        select d.value as extension, trim(concat_ws(' ', u.firstname, u.lastname)) as name,
          coalesce(nullif(active.displayname,''),active.profilename) as status,
          active.profilename as status_code,
          e.enabled, e.qstatus=1 as queues_global_logged_in,
          e.currentprofileoverride is not null and
            (e.overrideexpiresat is null or e.overrideexpiresat>now()) as temporary_override
        from users u join extension e on e.fkiddn=u.fkidextension
        join dn d on d.iddn=e.fkiddn
        left join fwdprofile active on active.idfwdprofile=case
          when e.currentprofileoverride is not null and
            (e.overrideexpiresat is null or e.overrideexpiresat>now())
          then e.currentprofileoverride else e.currentprofile end
        {where}
      ) x
    """


def get_user(extension):
    rows = query_json(user_sql(extension))
    if not rows:
        raise ApiError(404, "Poste inconnu")
    return rows[0]


def get_queues():
    return query_json("""
      select coalesce(json_agg(row_to_json(x) order by x.queue), '[]'::json) from (
        select d.value as queue, q.name,
          (select count(*) from queue2dn m where m.fkidqueue=q.fkiddn) as agent_count
        from queue q join dn d on d.iddn=q.fkiddn
      ) x
    """)


def get_agents(queue):
    queue = number(queue)
    return query_json(f"""
      select coalesce(json_agg(row_to_json(x) order by x.extension), '[]'::json) from (
        select ed.value as extension, trim(concat_ws(' ',u.firstname,u.lastname)) as name,
          m.is_logged_in as queue_logged_in, e.qstatus=1 as global_logged_in,
          (m.is_logged_in and e.qstatus=1 and e.enabled) as effective_logged_in,
          e.enabled
        from queue q join dn qd on qd.iddn=q.fkiddn
        join queue2dn m on m.fkidqueue=q.fkiddn
        join extension e on e.fkiddn=m.fkiddn
        join dn ed on ed.iddn=e.fkiddn
        left join users u on u.fkidextension=e.fkiddn
        where qd.value='{queue}'
      ) x
    """)


def get_memberships(extension):
    extension = number(extension)
    return query_json(f"""
      select coalesce(json_agg(row_to_json(x) order by x.queue), '[]'::json) from (
        select qd.value as queue, q.name, m.is_logged_in as queue_logged_in,
          e.qstatus=1 as global_logged_in,
          (m.is_logged_in and e.qstatus=1 and e.enabled) as effective_logged_in
        from extension e join dn ed on ed.iddn=e.fkiddn
        join queue2dn m on m.fkiddn=e.fkiddn
        join queue q on q.fkiddn=m.fkidqueue
        join dn qd on qd.iddn=q.fkiddn
        where ed.value='{extension}'
      ) x
    """)


def change_queue_individual(extension, queue, logged_in):
    """Use 3CX's local QueueAgent API, never a direct PBX database write."""
    number(extension)
    number(queue)
    if not isinstance(logged_in, bool):
        raise ApiError(400, "logged_in doit être booléen")
    before = get_memberships(extension)
    selected = next((item for item in before if item["queue"] == queue), None)
    if selected is None:
        raise ApiError(404, "Ce poste n'est pas membre de cette file")
    if selected["queue_logged_in"] == logged_in:
        return selected
    command = [
        "/opt/simple-api-3cx/dotnet/dotnet",
        "/opt/simple-api-3cx/queue_control/QueueControl.dll",
        "queue", extension, queue, "login" if logged_in else "logout",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(502, "Commande locale 3CX indisponible") from exc
    if result.returncode:
        raise ApiError(502, "3CX a refusé la modification de cette file")
    for _ in range(10):
        current = get_memberships(extension)
        selected = next((item for item in current if item["queue"] == queue), None)
        if selected and selected["queue_logged_in"] == logged_in:
            return selected
        time.sleep(0.2)
    raise ApiError(502, "3CX n'a pas confirmé la modification de cette file")


def parse_period(params):
    if "from" in params or "to" in params:
        if "from" not in params or "to" not in params:
            raise ApiError(400, "from et to sont requis ensemble")
        try:
            start = datetime.fromisoformat(params["from"][0].replace("Z", "+00:00"))
            end = datetime.fromisoformat(params["to"][0].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ApiError(400, "Dates ISO 8601 invalides") from exc
        if start.tzinfo is None or end.tzinfo is None:
            raise ApiError(400, "Les dates doivent inclure un fuseau horaire")
    else:
        now = datetime.now(PARIS)
        period = params.get("period", ["week"])[0]
        if period == "week":
            start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            raise ApiError(400, "period doit valoir week ou today")
        end = now
    if end <= start or end-start > timedelta(days=366):
        raise ApiError(400, "Période invalide ou trop longue")
    return start, end


def get_stats(queue, params):
    queue = number(queue)
    start, end = parse_period(params)
    a, b = start.isoformat(), end.isoformat()
    report = query_json(f"""
      select row_to_json(x) from (
        select received_count as received, answered_count as handled
        from call_cent_queue_team_proc('{queue}','{a}'::timestamptz,'{b}'::timestamptz,'0 seconds')
        where dn_number='{queue}' limit 1
      ) x
    """)
    if report is None:
        raise ApiError(404, "File inconnue")
    lost = query_json(f"""
      select row_to_json(x) from (
        select lost_count as not_handled
        from call_cent_queue_lost_calls_proc('{queue}','{a}'::timestamptz,'{b}'::timestamptz,'0 seconds')
        limit 1
      ) x
    """)
    hangups = query_json(f"""
      select count(*) from callcent_queuecalls
      where q_num='{queue}' and time_start>='{a}'::timestamptz
        and time_start<='{b}'::timestamptz
        and reason_faildesc='Caller dropped the call'
    """)
    return {
        "queue": queue, "from": a, "to": b,
        "received": report["received"], "handled": report["handled"],
        "not_handled": lost["not_handled"] if lost else 0,
        "caller_hangups": hangups,
        "definition": "not_handled includes caller hangups, maximum wait and no-agent cases",
    }


def sip_credentials(extension):
    extension = number(extension)
    result = query_json(f"""
      select row_to_json(x) from (
        select e.authid,e.authpswd from extension e
        join dn d on d.iddn=e.fkiddn where d.value='{extension}' limit 1
      ) x
    """)
    if not result or not result.get("authid") or not result.get("authpswd"):
        raise ApiError(404, "Identifiants SIP indisponibles pour ce poste")
    return result["authid"], result["authpswd"]


def md5(value):
    return hashlib.md5(value.encode()).hexdigest()


def sip_dial(extension, code, domain, target):
    """Originate a short authenticated local SIP call and wait for PBX completion."""
    authid, password = sip_credentials(extension)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        media.bind(("127.0.0.1", 0))
        sock.settimeout(2)
        port, media_port = sock.getsockname()[1], media.getsockname()[1]
        call_id, tag = uuid.uuid4().hex+"@localhost", uuid.uuid4().hex
        uri = f"sip:{code}@{domain}"
        to_header = f"<{uri}>"

        def send(method, seq, auth=None, to_value=None):
            branch = "z9hG4bK"+uuid.uuid4().hex
            lines = [
                f"{method} {uri} SIP/2.0",
                f"Via: SIP/2.0/UDP 127.0.0.1:{port};branch={branch};rport",
                "Max-Forwards: 70",
                f"From: <sip:{extension}@{domain}>;tag={tag}",
                f"To: {to_value or to_header}",
                f"Call-ID: {call_id}", f"CSeq: {seq} {method}",
                f"Contact: <sip:{extension}@127.0.0.1:{port}>",
                "User-Agent: Simple-API-3CX/0.1",
            ]
            if auth:
                lines.append(auth)
            body = ""
            if method == "INVITE":
                body = (
                    "v=0\r\n" "o=- 1 1 IN IP4 127.0.0.1\r\n" "s=Simple API 3CX\r\n"
                    "c=IN IP4 127.0.0.1\r\n" "t=0 0\r\n"
                    f"m=audio {media_port} RTP/AVP 0\r\n"
                    "a=rtpmap:0 PCMU/8000\r\n" "a=sendrecv\r\n"
                )
                lines.append("Content-Type: application/sdp")
            lines.append(f"Content-Length: {len(body.encode())}")
            sock.sendto(("\r\n".join(lines)+"\r\n\r\n"+body).encode(), (target, 5060))

        seq, challenged = 1, False
        send("INVITE", seq)
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            try:
                response = sock.recvfrom(8192)[0].decode(errors="replace")
            except socket.timeout:
                continue
            header_lines = response.split("\r\n\r\n",1)[0].split("\r\n")
            match = re.match(r"SIP/2.0 (\d{3})", header_lines[0])
            if not match:
                continue
            status = int(match.group(1))
            if status < 200:
                continue
            headers = {}
            for line in header_lines[1:]:
                if ":" in line:
                    key, value = line.split(":",1)
                    headers.setdefault(key.lower(),[]).append(value.strip())
            if status in (401,407) and not challenged:
                name = "proxy-authenticate" if status==407 else "www-authenticate"
                if name not in headers:
                    raise ApiError(502, "Défi SIP incomplet")
                values = dict(re.findall(r'(\w+)="([^"]*)"', headers[name][0]))
                realm, nonce = values.get("realm"), values.get("nonce")
                if not realm or not nonce:
                    raise ApiError(502, "Défi SIP invalide")
                ha1 = md5(f"{authid}:{realm}:{password}")
                ha2 = md5(f"INVITE:{uri}")
                response_hash = md5(f"{ha1}:{nonce}:{ha2}")
                kind = "Proxy-Authorization" if status==407 else "Authorization"
                auth = (
                    f'{kind}: Digest username="{authid}", realm="{realm}", '
                    f'nonce="{nonce}", uri="{uri}", response="{response_hash}", algorithm=MD5'
                )
                challenged, seq = True, seq+1
                send("INVITE", seq, auth)
                continue
            if status == 200:
                to_header = headers.get("to",[to_header])[0]
                send("ACK",seq,to_value=to_header)
                time.sleep(3)
                send("BYE",seq+1,to_value=to_header)
                return
            raise ApiError(502, f"3CX a refusé le code SIP : {status}")
        raise ApiError(504, "Délai d'appel SIP dépassé")
    finally:
        sock.close()
        media.close()


def client_ip(handler):
    remote = ipaddress.ip_address(handler.client_address[0])
    if remote.is_loopback and handler.headers.get("X-Real-IP"):
        try:
            return ipaddress.ip_address(handler.headers["X-Real-IP"])
        except ValueError:
            raise ApiError(400, "Adresse client invalide")
    return remote


def authorize(handler, config, scope, query):
    header = handler.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else None
    if not token and query.get("ticket"):
        ticket = query["ticket"][0]
        if ":" not in ticket:
            raise ApiError(401, "Ticket invalide")
        ticket_id, token = ticket.split(":",1)
        entry = config.get("tickets",{}).get(ticket_id)
        if not entry or not hmac.compare_digest(entry["hash"],digest(token)):
            raise ApiError(401, "Ticket invalide")
        if entry["expires"] < int(time.time()):
            raise ApiError(401, "Ticket expiré")
        return ("ticket", ticket_id, entry)
    if not token and query.get("link"):
        link = query["link"][0]
        if ":" not in link:
            raise ApiError(401, "Lien invalide")
        link_id, token = link.split(":",1)
        entry = config.get("links",{}).get(link_id)
        if not entry or not hmac.compare_digest(entry["hash"],digest(token)):
            raise ApiError(401, "Lien invalide")
        ip = client_ip(handler)
        if not any(ip in ipaddress.ip_network(cidr, strict=False) for cidr in config["allowed_ips"]):
            raise ApiError(403, "Adresse IP non autorisée")
        return ("link",link_id,entry)
    ip = client_ip(handler)
    if not any(ip in ipaddress.ip_network(cidr, strict=False) for cidr in config["allowed_ips"]):
        raise ApiError(403, "Adresse IP non autorisée")
    if not token:
        raise ApiError(401, "Clé API requise")
    token_hash = digest(token)
    for key_id, entry in config.get("keys",{}).items():
        if hmac.compare_digest(entry["hash"],token_hash):
            if scope not in entry["scopes"] and "admin" not in entry["scopes"]:
                raise ApiError(403, "Droit insuffisant")
            return ("key",key_id,entry)
    raise ApiError(401, "Clé API invalide")


def authorize_automation(handler, config, query):
    ip = client_ip(handler)
    if not any(ip in ipaddress.ip_network(cidr, strict=False) for cidr in config["allowed_ips"]):
        raise ApiError(403, "Adresse IP non autorisée")
    credentials = query.get("auth", [])
    if len(credentials) != 1 or ":" not in credentials[0]:
        raise ApiError(401, "Clé d'automatisation requise")
    name, secret = credentials[0].split(":", 1)
    entry = config.get("automation_keys", {}).get(name)
    if not entry or not hmac.compare_digest(entry["hash"], digest(secret)):
        raise ApiError(401, "Clé d'automatisation invalide")


def one_param(query, name, required=True):
    values = query.get(name, [])
    if len(values) != 1 or not values[0]:
        if required or values:
            raise ApiError(400, f"Paramètre {name} invalide ou manquant")
        return None
    return values[0]


class Handler(BaseHTTPRequestHandler):
    server_version = "SimpleAPI3CX/0.1"

    def log_message(self, format_string, *args):
        # URLs may contain one-time browser tickets. Never log request targets.
        sys.stderr.write(f"simple-api-3cx {self.client_address[0]} {self.command} {getattr(self,'_safe_route','unknown')}\n")

    def respond(self, status, data):
        payload = json.dumps(data,ensure_ascii=False,separators=(",",":")).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def body(self):
        try:
            size = int(self.headers.get("Content-Length","0"))
        except ValueError as exc:
            raise ApiError(400,"Content-Length invalide") from exc
        if size < 1 or size > 4096:
            raise ApiError(400,"Corps JSON requis (maximum 4 Kio)")
        try:
            value = json.loads(self.rfile.read(size))
        except (ValueError,UnicodeDecodeError) as exc:
            raise ApiError(400,"JSON invalide") from exc
        if not isinstance(value,dict):
            raise ApiError(400,"Objet JSON requis")
        return value

    def handle_request(self):
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/")
        self._safe_route = path
        params = parse_qs(parsed.query)
        config = load_config()
        base = "/simple-api-3cx/v1"
        if path == base+"/health" and self.command=="GET":
            return self.respond(200,{"service":"simple-api-3cx","status":"ok"})
        if path == base+"/help" and self.command=="GET":
            return self.respond(200,{
                "auth":"Authorization: Bearer <key>; browser actions use a ticket or permanent link; /automation uses auth=<name>:<secret> and an allowed IP",
                "routes":[
                    "GET /users", "GET /users/{extension}",
                    "GET /users/{extension}/queues", "GET /queues",
                    "GET /queues/{queue}/agents",
                    "GET /queues/{queue}/stats?period=week|today",
                    "GET /queues/{queue}/stats?from=ISO8601&to=ISO8601",
                    "POST /users/{extension}/status {status: available|away|dnd|custom1|custom2}",
                    "POST /users/{extension}/queues/global {logged_in: true|false}",
                    "POST /queues/{queue}/agents/{extension}/login {logged_in: true|false}",
                    "GET /browser/queues/{queue}/agents/{extension}/login|logout?link=<permanent-link>",
                    "GET /automation?poste={extension}&action=available|away|dnd|custom1|custom2&auth={name}:{secret}",
                    "GET /automation?poste={extension}&file={queue}&action=login|logout&auth={name}:{secret}",
                ],
            })
        if not path.startswith(base):
            raise ApiError(404,"Route inconnue")
        route = path[len(base):]
        if route == "/automation" and self.command == "GET":
            authorize_automation(self, config, params)
            poste = number(one_param(params, "poste"))
            action = one_param(params, "action")
            file_number = one_param(params, "file", required=False)
            if action in PROFILE_CODES and file_number is None:
                return self.change_status(poste, action, config)
            if action in ("login", "logout") and file_number is not None:
                return self.change_queue_individual(poste, number(file_number), action == "login")
            raise ApiError(400, "Action invalide : statut sans file, ou login/logout avec file")
        scope = "control" if self.command=="POST" or route.startswith("/browser/") else "read"
        identity = authorize(self,config,scope,params)
        if identity[0]=="ticket":
            ticket_entry = identity[2]
            if self.command!="GET" or route!=ticket_entry["route"]:
                raise ApiError(403,"Ticket non valable pour cette action")
            with WRITE_LOCK:
                fresh = load_config()
                fresh.get("tickets",{}).pop(identity[1],None)
                save_config(fresh)
        if identity[0]=="link":
            if self.command!="GET" or route!=identity[2]["route"]:
                raise ApiError(403,"Lien non valable pour cette action")
        if self.command=="GET":
            if route=="/users":
                return self.respond(200,{"users":query_json(user_sql())})
            match = re.fullmatch(r"/users/([0-9]+)/queues",route)
            if match:
                get_user(match.group(1))
                return self.respond(200,{"extension":match.group(1),"queues":get_memberships(match.group(1))})
            match = re.fullmatch(r"/users/([0-9]+)",route)
            if match:
                return self.respond(200,get_user(match.group(1)))
            if route=="/queues":
                return self.respond(200,{"queues":get_queues()})
            match = re.fullmatch(r"/queues/([0-9]+)/agents",route)
            if match:
                return self.respond(200,{"queue":match.group(1),"agents":get_agents(match.group(1))})
            match = re.fullmatch(r"/queues/([0-9]+)/stats",route)
            if match:
                return self.respond(200,get_stats(match.group(1),params))
            match = re.fullmatch(r"/browser/status/([0-9]+)/(available|away|dnd|custom1|custom2)",route)
            if match and identity[0] in ("ticket","link"):
                return self.change_status(match.group(1),match.group(2),config)
            match = re.fullmatch(r"/browser/queues/([0-9]+)/agents/([0-9]+)/(login|logout)",route)
            if match and identity[0]=="link":
                return self.change_queue_individual(match.group(2),match.group(1),match.group(3)=="login")
        if self.command=="POST":
            match = re.fullmatch(r"/users/([0-9]+)/status",route)
            if match:
                status = self.body().get("status")
                return self.change_status(match.group(1),status,config)
            match = re.fullmatch(r"/users/([0-9]+)/queues/global",route)
            if match:
                logged_in = self.body().get("logged_in")
                return self.change_queue_global(match.group(1),logged_in,config)
            match = re.fullmatch(r"/queues/([0-9]+)/agents/([0-9]+)/login",route)
            if match:
                logged_in = self.body().get("logged_in")
                return self.change_queue_individual(match.group(2),match.group(1),logged_in)
        raise ApiError(404,"Route inconnue")

    def change_status(self,extension,status,config):
        number(extension)
        if status not in PROFILE_CODES:
            raise ApiError(400,"Statut invalide")
        get_user(extension)
        code, expected = PROFILE_CODES[status]
        sip_dial(extension,code,config["sip_domain"],config["sip_target"])
        actual = get_user(extension)
        if actual["status_code"]!=expected:
            raise ApiError(502,"3CX a accepté l'appel mais le statut n'a pas changé")
        return self.respond(200,actual)

    def change_queue_global(self,extension,logged_in,config):
        number(extension)
        if not isinstance(logged_in,bool):
            raise ApiError(400,"logged_in doit être booléen")
        get_user(extension)
        sip_dial(extension,"*62" if logged_in else "*63",config["sip_domain"],config["sip_target"])
        actual = get_user(extension)
        if actual["queues_global_logged_in"]!=logged_in:
            raise ApiError(502,"3CX a accepté l'appel mais la connexion aux files n'a pas changé")
        return self.respond(200,{"extension":extension,"queues_global_logged_in":logged_in,"queues":get_memberships(extension)})

    def change_queue_individual(self,extension,queue,logged_in):
        with WRITE_LOCK:
            selected = change_queue_individual(extension,queue,logged_in)
        return self.respond(200,{"extension":extension,"queue":queue,
            "queue_logged_in":selected["queue_logged_in"],
            "global_logged_in":selected["global_logged_in"],
            "effective_logged_in":selected["effective_logged_in"]})

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        try:
            self.handle_request()
        except ApiError as exc:
            self.respond(exc.status,{"error":exc.message})
        except Exception:
            self.respond(500,{"error":"Erreur interne"})


def cli():
    parser = argparse.ArgumentParser(description="Simple API 3cx")
    sub = parser.add_subparsers(dest="command",required=True)
    sub.add_parser("serve")
    sub.add_parser("init")
    key = sub.add_parser("key")
    key.add_argument("action",choices=["create","list","revoke"])
    key.add_argument("name",nargs="?")
    key.add_argument("--scopes",default="read")
    allow = sub.add_parser("allow")
    allow.add_argument("action",choices=["add","remove","list"])
    allow.add_argument("cidr",nargs="?")
    url = sub.add_parser("url")
    url.add_argument("extension")
    url.add_argument("status",choices=PROFILE_CODES)
    url.add_argument("--minutes",type=int,default=15)
    link = sub.add_parser("link")
    link.add_argument("action",choices=["create","list","revoke"])
    link.add_argument("name",nargs="?")
    link.add_argument("extension",nargs="?")
    link.add_argument("status",nargs="?",choices=PROFILE_CODES)
    queue_link = sub.add_parser("queue-link")
    queue_link.add_argument("name")
    queue_link.add_argument("queue")
    queue_link.add_argument("extension")
    queue_link.add_argument("action",choices=["login","logout"])
    automation = sub.add_parser("automation")
    automation.add_argument("action", choices=["create", "list", "revoke"])
    automation.add_argument("name", nargs="?")
    args = parser.parse_args()
    if args.command=="init":
        if CONFIG_PATH.exists():
            raise SystemExit("Configuration déjà présente")
        config={
            "sip_domain":os.environ.get("SIMPLE_API_3CX_DOMAIN","pbx.example.invalid"),
            "sip_target":"127.0.0.1", "listen_host":"127.0.0.1", "listen_port":18081,
            "public_url":os.environ.get("SIMPLE_API_3CX_URL","https://pbx.example.invalid"),
            "allowed_ips":["127.0.0.1/32","::1/128"],"keys":{},"tickets":{},"links":{},"automation_keys":{},
        }
        save_config(config)
        print("Configuration créée ; accès IP limité au serveur local.")
        return
    if args.command=="serve":
        config=load_config()
        server=ThreadingHTTPServer((config["listen_host"],config["listen_port"]),Handler)
        server.serve_forever()
        return
    config=load_config()
    if args.command=="key":
        if args.action=="list":
            for name,entry in config["keys"].items():
                print(name,",".join(entry["scopes"]))
            return
        if not args.name or not re.fullmatch(r"[A-Za-z0-9_.-]{1,50}",args.name):
            raise SystemExit("Nom de clé requis (lettres, chiffres, _, -, .)")
        if args.action=="revoke":
            if args.name not in config["keys"]:
                raise SystemExit("Clé inconnue")
            del config["keys"][args.name]
            save_config(config)
            print("Clé révoquée")
            return
        scopes=sorted(set(args.scopes.split(",")))
        if not set(scopes)<= {"read","control","admin"} or not scopes:
            raise SystemExit("Scopes valides : read,control,admin")
        if args.name in config["keys"]:
            raise SystemExit("Nom de clé déjà utilisé")
        token=secrets.token_urlsafe(32)
        config["keys"][args.name]={"hash":digest(token),"scopes":scopes,"created":int(time.time())}
        save_config(config)
        print(f"Clé {args.name} (affichée une seule fois) : {token}")
        return
    if args.command=="automation":
        if args.action=="list":
            print("\n".join(config.get("automation_keys", {})))
            return
        if not args.name or not re.fullmatch(r"[A-Za-z0-9_.-]{1,50}", args.name):
            raise SystemExit("Nom d'automatisation requis (lettres, chiffres, _, -, .)")
        if args.action=="revoke":
            if args.name not in config.get("automation_keys", {}):
                raise SystemExit("Automatisation inconnue")
            del config["automation_keys"][args.name]
            save_config(config)
            print("Clé d'automatisation révoquée")
            return
        if args.name in config.get("automation_keys", {}):
            raise SystemExit("Nom d'automatisation déjà utilisé")
        secret = secrets.token_urlsafe(32)
        config.setdefault("automation_keys", {})[args.name] = {"hash": digest(secret), "created": int(time.time())}
        save_config(config)
        base = f"{config['public_url']}/simple-api-3cx/v1/automation"
        print(f"Statut : {base}?poste=10&action=away&auth={args.name}:{secret}")
        print(f"File : {base}?poste=10&file=81&action=login&auth={args.name}:{secret}")
        print("Remplacer poste, file et action selon le besoin. Secret affiché une seule fois.")
        return
    if args.command=="allow":
        if args.action=="list":
            print("\n".join(config["allowed_ips"]))
            return
        if not args.cidr:
            raise SystemExit("Adresse IP ou CIDR requis")
        cidr=str(ipaddress.ip_network(args.cidr,strict=False))
        if args.action=="add":
            if cidr not in config["allowed_ips"]:
                config["allowed_ips"].append(cidr)
        elif args.action=="remove":
            if cidr not in config["allowed_ips"]:
                raise SystemExit("IP/CIDR absent de la liste")
            config["allowed_ips"].remove(cidr)
        save_config(config)
        print("Liste IP mise à jour")
        return
    if args.command=="url":
        number(args.extension)
        if not 1<=args.minutes<=60:
            raise SystemExit("Durée de 1 à 60 minutes")
        ticket_id=uuid.uuid4().hex
        secret=secrets.token_urlsafe(24)
        route=f"/browser/status/{args.extension}/{args.status}"
        config["tickets"][ticket_id]={"hash":digest(secret),"route":route,"expires":int(time.time())+60*args.minutes}
        save_config(config)
        print(f"{config['public_url']}/simple-api-3cx/v1{route}?ticket={ticket_id}:{secret}")
        return
    if args.command=="link":
        if args.action=="list":
            for name,entry in config.get("links",{}).items():
                print(name,entry["route"])
            return
        if not args.name or not re.fullmatch(r"[A-Za-z0-9_.-]{1,50}",args.name):
            raise SystemExit("Nom de lien requis (lettres, chiffres, _, -, .)")
        if args.action=="revoke":
            if args.name not in config.get("links",{}):
                raise SystemExit("Lien inconnu")
            del config["links"][args.name]
            save_config(config)
            print("Lien révoqué")
            return
        if not args.extension or not args.status:
            raise SystemExit("Créer un lien : link create NOM POSTE STATUT")
        number(args.extension)
        if args.name in config.get("links",{}):
            raise SystemExit("Nom de lien déjà utilisé")
        secret=secrets.token_urlsafe(32)
        route=f"/browser/status/{args.extension}/{args.status}"
        config.setdefault("links",{})[args.name]={"hash":digest(secret),"route":route,"created":int(time.time())}
        save_config(config)
        print(f"{config['public_url']}/simple-api-3cx/v1{route}?link={args.name}:{secret}")
        return
    if args.command=="queue-link":
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,50}",args.name):
            raise SystemExit("Nom de lien invalide")
        number(args.queue)
        number(args.extension)
        if args.name in config.get("links",{}):
            raise SystemExit("Nom de lien déjà utilisé")
        secret=secrets.token_urlsafe(32)
        route=f"/browser/queues/{args.queue}/agents/{args.extension}/{args.action}"
        config.setdefault("links",{})[args.name]={"hash":digest(secret),"route":route,"created":int(time.time())}
        save_config(config)
        print(f"{config['public_url']}/simple-api-3cx/v1{route}?link={args.name}:{secret}")


if __name__=="__main__":
    cli()
