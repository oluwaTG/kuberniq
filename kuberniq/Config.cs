using System.Text.Json;

namespace Kuberniq;

/// <summary>
/// Saved MCP server connection — stored at ~/.kuberniq/config.json
/// </summary>
record KuberniqConfig(string ServerUrl, string? DefaultCluster = null);

static class KuberniqConfigManager
{
    private static string ConfigPath =>
        Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            ".kuberniq", "config.json");

    public static KuberniqConfig? Load()
    {
        if (!File.Exists(ConfigPath)) return null;
        try
        {
            return JsonSerializer.Deserialize<KuberniqConfig>(
                File.ReadAllText(ConfigPath),
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
        }
        catch { return null; }
    }

    public static void Save(KuberniqConfig config)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(ConfigPath)!);
        File.WriteAllText(ConfigPath,
            JsonSerializer.Serialize(config, new JsonSerializerOptions { WriteIndented = true }));
    }

    public static void Delete()
    {
        if (File.Exists(ConfigPath)) File.Delete(ConfigPath);
    }

    public static KuberniqConfig LoadOrFail()
    {
        var cfg = Load();
        if (cfg is null)
            throw new InvalidOperationException(
                "Not logged in. Run [cyan]kuberniq login <server-url>[/] first.");
        return cfg;
    }
}

