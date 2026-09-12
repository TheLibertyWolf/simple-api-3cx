using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;
using TCX.Configuration;

// The local 3CX configuration API is the supported writer for individual
// QueueAgent.QueueStatus. No direct PBX database updates are made here.
sealed class QuietLogger : PhoneSystem.ILog
{
    public void Critical(string message, params object[] args) { }
    public void Error(string message, params object[] args) { }
    public void Exception(Exception error) { }
    public void Info(string message, params object[] args) { }
    public void Trace(string message, params object[] args) { }
    public void Dispose() { }
}

static class Program
{
    static Dictionary<string, string> ReadConfService()
    {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var inSection = false;
        foreach (var raw in File.ReadLines("/var/lib/3cxpbx/Bin/3CXPhoneSystem.ini"))
        {
            var line = raw.Trim();
            if (line.StartsWith('['))
            {
                inSection = line.Equals("[ConfService]", StringComparison.OrdinalIgnoreCase);
                continue;
            }
            if (!inSection || line.StartsWith('#') || line.StartsWith(';')) continue;
            var separator = line.IndexOf('=');
            if (separator > 0) result[line[..separator].Trim()] = line[(separator + 1)..].Trim();
        }
        return result;
    }

    static int Main(string[] args)
    {
        if (args.Length != 4 || args[0] != "queue" ||
            !args[1].All(char.IsAsciiDigit) || !args[2].All(char.IsAsciiDigit) ||
            (args[3] != "read" && args[3] != "login" && args[3] != "logout"))
        {
            Console.Error.WriteLine("Usage: queue <extension> <queue> read|login|logout");
            return 2;
        }
        AssemblyLoadContext.Default.Resolving += (_, name) =>
        {
            var path = Path.Combine("/usr/lib/3cxpbx", name.Name + ".dll");
            return File.Exists(path) ? AssemblyLoadContext.Default.LoadFromAssemblyPath(path) : null;
        };
        PhoneSystem.Logger = new QuietLogger();
        PhoneSystem? ps = null;
        try
        {
            var ini = ReadConfService();
            ps = PhoneSystem.Reset("SimpleAPI3CXQueue" + Guid.NewGuid().ToString("N"),
                "127.0.0.1", int.Parse(ini["ConfPort"]), ini["confUser"], ini["confPass"]);
            ps.WaitForConnect(TimeSpan.FromSeconds(15));
            var extension = ps.GetDNByNumber(args[1]) as Extension;
            if (extension == null)
            {
                Console.Error.WriteLine("Poste inconnu");
                return 3;
            }
            var membership = extension.QueueMembership.FirstOrDefault(item => item.Queue.Number == args[2]);
            if (membership == null)
            {
                Console.Error.WriteLine("Le poste n'est pas membre de cette file");
                return 4;
            }
            if (args[3] != "read")
            {
                var desired = args[3] == "login" ? QueueStatusType.LoggedIn : QueueStatusType.LoggedOut;
                if (membership.QueueStatus != desired)
                {
                    membership.QueueStatus = desired;
                    extension.Save();
                }
            }
            Console.WriteLine(JsonSerializer.Serialize(new {
                extension = args[1], queue = args[2],
                queue_logged_in = membership.QueueStatus == QueueStatusType.LoggedIn,
                global_logged_in = extension.QueueStatus == QueueStatusType.LoggedIn
            }));
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.GetType().Name + ": " + error.Message);
            return 5;
        }
        finally
        {
            ps?.Disconnect();
        }
    }
}
