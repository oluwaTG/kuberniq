using System.ComponentModel;
using System.Net.Http.Json;
using Spectre.Console;
using Spectre.Console.Cli;
using Kuberniq;

// ── Entry point ───────────────────────────────────────────────────────────────
var app = new CommandApp();
app.Configure(config =>
{
    config.SetApplicationName("kuberniq");
    config.SetApplicationVersion("1.0.0");
    config.PropagateExceptions();

    config.AddCommand<LoginCommand>("login")
          .WithDescription("Authenticate with an MCP server and save the connection.")
          .WithExample(["login", "http://mcp-server.example.com"]);

    config.AddCommand<LogoutCommand>("logout")
          .WithDescription("Remove the saved MCP server connection.");

    config.AddBranch("cluster", branch =>
    {
        branch.SetDescription("Manage remote clusters monitored by the MCP server.");

        branch.AddCommand<ClusterAddCommand>("add")
              .WithDescription(
                  "Register a remote cluster: creates a read-only ServiceAccount in the " +
                  "target cluster, then calls POST /clusters on the MCP server.")
              .WithExample(["cluster", "add", "prod", "--context", "prod-aks"])
              .WithExample(["cluster", "add", "staging"]);   // interactive context selector

        branch.AddCommand<ClusterListCommand>("list")
              .WithDescription("List all clusters registered with the MCP server.");

        branch.AddCommand<ClusterShowCommand>("show")
              .WithDescription("Show detailed information about a registered cluster.")
              .WithExample(["cluster", "show", "prod"]);

        branch.AddCommand<ClusterPingCommand>("ping")
              .WithDescription("Check latency and reachability of a registered cluster.")
              .WithExample(["cluster", "ping", "prod"]);

        branch.AddCommand<ClusterSetDefaultCommand>("set-default")
              .WithDescription("Set the default cluster used when no --cluster flag is provided.")
              .WithExample(["cluster", "set-default", "prod"]);

        branch.AddCommand<ClusterRemoveCommand>("remove")
              .WithDescription("Unregister a cluster from the MCP server.")
              .WithExample(["cluster", "remove", "prod"]);
    });
});

try
{
    return app.Run(args);
}
catch (InvalidOperationException ex)
{
    AnsiConsole.MarkupLine($"[red]✗[/] {ex.Message}");
    return 1;
}
catch (Exception ex)
{
    AnsiConsole.MarkupLine($"[red]✗[/] Unexpected error: {ex.Message}");
    return 1;
}

// ── Commands ──────────────────────────────────────────────────────────────────

// ── login ──────────────────────────────────────────────────────────────────────
sealed class LoginSettings : CommandSettings
{
    [CommandArgument(0, "<server-url>")]
    [Description("URL of the MCP server, e.g. http://mcp-server.example.com")]
    public string ServerUrl { get; init; } = "";
}

sealed class LoginCommand : AsyncCommand<LoginSettings>
{
    public override async Task<int> ExecuteAsync(CommandContext ctx, LoginSettings s)
    {
        var url = s.ServerUrl.TrimEnd('/');
        AnsiConsole.MarkupLine($"Connecting to [cyan]{url}[/]...");

        try
        {
            using var http = new HttpClient();
            var resp = await http.GetAsync($"{url}/health");
            resp.EnsureSuccessStatusCode();
        }
        catch (Exception ex)
        {
            AnsiConsole.MarkupLine($"[red]✗[/] Cannot reach MCP server: {ex.Message}");
            return 1;
        }

        KuberniqConfigManager.Save(new KuberniqConfig(url));
        AnsiConsole.MarkupLine($"[green]✓[/] Authenticated. Config saved to [grey]~/.kubeai/config.json[/].");
        AnsiConsole.MarkupLine("  Run [cyan]kuberniq cluster add <name>[/] to register your first cluster.");
        return 0;
    }
}

// ── logout ─────────────────────────────────────────────────────────────────────
sealed class LogoutCommand : Command
{
    public override int Execute(CommandContext ctx)
    {
        KuberniqConfigManager.Delete();
        AnsiConsole.MarkupLine("[green]✓[/] Logged out. Config removed.");
        return 0;
    }
}

// ── cluster add ────────────────────────────────────────────────────────────────
sealed class ClusterAddSettings : CommandSettings
{
    [CommandArgument(0, "<name>")]
    [Description("Friendly name used in ?cluster=<name> queries on every MCP endpoint")]
    public string Name { get; init; } = "";

    [CommandOption("-c|--context")]
    [Description("Kubeconfig context for the target cluster. " +
                 "If omitted, kubeai shows an interactive selection menu.")]
    public string? Context { get; init; }

    [CommandOption("--sa-name")]
    [Description("Name of the ServiceAccount to create in the target cluster (default: kubeai)")]
    [DefaultValue("kubeai")]
    public string SaName { get; init; } = "kubeai";

    [CommandOption("--sa-namespace")]
    [Description("Namespace for the ServiceAccount (default: kube-system)")]
    [DefaultValue("kube-system")]
    public string SaNamespace { get; init; } = "kube-system";

    [CommandOption("--skip-rbac")]
    [Description("Skip ServiceAccount and RBAC creation (use when they already exist)")]
    public bool SkipRbac { get; init; }
}

sealed class ClusterAddCommand : AsyncCommand<ClusterAddSettings>
{
    public override async Task<int> ExecuteAsync(CommandContext ctx, ClusterAddSettings s)
    {
        var cfg = KuberniqConfigManager.LoadOrFail();

        // ── Resolve kubeconfig context ─────────────────────────────────────────
        var context = s.Context;
        if (string.IsNullOrWhiteSpace(context))
        {
            var contexts = ClusterRegistrar.ListKubeconfigContexts();
            if (contexts.Count == 0)
            {
                AnsiConsole.MarkupLine(
                    "[red]✗[/] No kubeconfig contexts found. " +
                    "Pass [cyan]--context <name>[/] explicitly.");
                return 1;
            }
            context = AnsiConsole.Prompt(
                new SelectionPrompt<string>()
                    .Title("Select the [cyan]kubeconfig context[/] for [bold]" + s.Name + "[/]:")
                    .PageSize(10)
                    .AddChoices(contexts));
        }

        // ── Set up RBAC + token in target cluster ──────────────────────────────
        string server = "", caData = "", token = "";

        await AnsiConsole.Status()
            .Spinner(Spinner.Known.Dots)
            .SpinnerStyle(Style.Parse("cyan"))
            .StartAsync(
                $"Preparing cluster [bold]{s.Name}[/] (context: [grey]{context}[/])...",
                async statusCtx =>
                {
                    var progress = new Progress<string>(
                        msg => statusCtx.Status($"[grey]{Markup.Escape(msg)}[/]"));

                    (server, caData, token) = await ClusterRegistrar.SetupAsync(
                        context!,
                        s.SaName,
                        s.SaNamespace,
                        s.SkipRbac,
                        progress);
                });

        AnsiConsole.MarkupLine($"  [green]✓[/] ServiceAccount and RBAC ready in [grey]{context}[/]");

        // ── Register with the MCP server ───────────────────────────────────────
        AnsiConsole.Markup($"  Registering with MCP server at [cyan]{cfg.ServerUrl}[/]... ");

        try
        {
            using var http = new HttpClient();
            var resp = await http.PostAsJsonAsync(
                $"{cfg.ServerUrl}/clusters",
                new { name = s.Name, server, caData, token });

            if (!resp.IsSuccessStatusCode)
            {
                var body = await resp.Content.ReadAsStringAsync();
                AnsiConsole.MarkupLine($"\n[red]✗[/] MCP server returned {(int)resp.StatusCode}: {Markup.Escape(body)}");
                return 1;
            }
        }
        catch (Exception ex)
        {
            AnsiConsole.MarkupLine($"\n[red]✗[/] {Markup.Escape(ex.Message)}");
            return 1;
        }

        AnsiConsole.MarkupLine("[green]✓[/]");
        AnsiConsole.WriteLine();

        var panel = new Panel(
            $"[bold green]{s.Name}[/] is now registered with the MCP server.\n\n" +
            $"Append [cyan]?cluster={s.Name}[/] to any endpoint:\n" +
            $"  [grey]{cfg.ServerUrl}/namespaces?cluster={s.Name}[/]\n" +
            $"  [grey]{cfg.ServerUrl}/namespaces/default/pods?cluster={s.Name}[/]\n\n" +
            $"List all clusters:  [cyan]kuberniq cluster list[/]\n" +
            $"Remove later:       [cyan]kuberniq cluster remove {s.Name}[/]")
        {
            Header  = new PanelHeader(" Cluster registered "),
            Padding = new Padding(1, 0)
        };
        AnsiConsole.Write(panel);
        return 0;
    }
}

// ── cluster list ───────────────────────────────────────────────────────────────
sealed class ClusterListCommand : AsyncCommand
{
    public override async Task<int> ExecuteAsync(CommandContext ctx)
    {
        var cfg = KuberniqConfigManager.LoadOrFail();

        List<ClusterInfo> clusters;
        try
        {
            using var http = new HttpClient();
            clusters = await http.GetFromJsonAsync<List<ClusterInfo>>(
                           $"{cfg.ServerUrl}/clusters",
                           new System.Text.Json.JsonSerializerOptions
                               { PropertyNameCaseInsensitive = true })
                       ?? [];
        }
        catch (Exception ex)
        {
            AnsiConsole.MarkupLine($"[red]✗[/] Could not reach MCP server: {Markup.Escape(ex.Message)}");
            return 1;
        }

        if (clusters.Count == 0)
        {
            AnsiConsole.MarkupLine("[yellow]No clusters registered.[/]  Run [cyan]kuberniq cluster add <name>[/].");
            return 0;
        }

        var table = new Table()
            .Title($"Clusters  ([grey]{Markup.Escape(cfg.ServerUrl)}[/])")
            .Border(TableBorder.Rounded)
            .AddColumn(new TableColumn("Name").LeftAligned())
            .AddColumn(new TableColumn("Type").Centered())
            .AddColumn(new TableColumn("?cluster= query param").LeftAligned());

        foreach (var c in clusters)
        {
            var type = c.IsLocal
                ? "[blue]local (in-cluster)[/]"
                : "[green]remote[/]";
            var param = c.IsLocal
                ? "[grey](omit for local)[/]"
                : $"[cyan]?cluster={c.Name}[/]";
            table.AddRow(c.Name, type, param);
        }

        AnsiConsole.Write(table);
        return 0;
    }

    record ClusterInfo(string Name, bool IsLocal);
}

// ── cluster show ───────────────────────────────────────────────────────────────
sealed class ClusterShowSettings : CommandSettings
{
    [CommandArgument(0, "<name>")]
    [Description("Name of the cluster to inspect")]
    public string Name { get; init; } = "";
}

sealed class ClusterShowCommand : AsyncCommand<ClusterShowSettings>
{
    private static readonly System.Text.Json.JsonSerializerOptions JsonOpts =
        new() { PropertyNameCaseInsensitive = true };

    public override async Task<int> ExecuteAsync(CommandContext ctx, ClusterShowSettings s)
    {
        var cfg = KuberniqConfigManager.LoadOrFail();
        using var http = new HttpClient();

        // ── 1. Basic cluster record ────────────────────────────────────────────
        ClusterDetail? detail;
        try
        {
            var resp = await http.GetAsync($"{cfg.ServerUrl}/clusters/{s.Name}");
            if (resp.StatusCode == System.Net.HttpStatusCode.NotFound)
            {
                AnsiConsole.MarkupLine($"[red]✗[/] Cluster [bold]{s.Name}[/] is not registered.");
                AnsiConsole.MarkupLine("  Run [cyan]kuberniq cluster list[/] to see registered clusters.");
                return 1;
            }
            resp.EnsureSuccessStatusCode();
            detail = await resp.Content.ReadFromJsonAsync<ClusterDetail>(JsonOpts);
        }
        catch (Exception ex)
        {
            AnsiConsole.MarkupLine($"[red]✗[/] Could not reach MCP server: {Markup.Escape(ex.Message)}");
            return 1;
        }

        if (detail is null)
        {
            AnsiConsole.MarkupLine("[red]✗[/] Empty response from MCP server.");
            return 1;
        }

        var clusterParam = detail.IsLocal ? "" : $"?cluster={s.Name}";

        // ── 2. Fetch in parallel: cluster/info + namespaces ────────────────────
        ClusterInfoResponse? info = null;
        string[]? namespaces = null;
        string reachability;

        await AnsiConsole.Status()
            .Spinner(Spinner.Known.Dots)
            .SpinnerStyle(Style.Parse("cyan"))
            .StartAsync($"Querying cluster [bold]{s.Name}[/]...", async _ =>
            {
                try
                {
                    var infoTask  = http.GetFromJsonAsync<ClusterInfoResponse>(
                                        $"{cfg.ServerUrl}/cluster/info{clusterParam}", JsonOpts);
                    var nsTask    = http.GetFromJsonAsync<string[]>(
                                        $"{cfg.ServerUrl}/namespaces{clusterParam}", JsonOpts);
                    await Task.WhenAll(infoTask, nsTask);
                    info       = infoTask.Result;
                    namespaces = nsTask.Result;
                }
                catch { /* handled below via null check */ }
            });

        // ── 3. Determine reachability ──────────────────────────────────────────
        if (info is not null)
            reachability = "[green]✓ reachable[/]";
        else
            reachability = "[red]✗ unreachable[/]";

        // ── 4. Build the display panel ─────────────────────────────────────────
        var grid = new Grid().AddColumn(new GridColumn().NoWrap())
                             .AddColumn(new GridColumn());

        void Row(string label, string value) =>
            grid.AddRow($"  [grey]{label,-16}[/]", value);

        var clusterType = detail.IsLocal
            ? "[blue]local (in-cluster)[/]"
            : "[green]remote[/]";

        Row("Name",        $"[bold]{Markup.Escape(detail.Name)}[/]");
        Row("Type",        clusterType);
        Row("Server",      detail.IsLocal ? "[grey]in-cluster[/]" : $"[grey]{Markup.Escape(detail.Server ?? "unknown")}[/]");
        Row("Status",      reachability);

        if (info is not null)
        {
            Row("K8s Version",  $"[cyan]{Markup.Escape(info.Version ?? "—")}[/]");
            var readyNodes = info.Nodes?.Count(n => n.Ready?.Equals("True", StringComparison.OrdinalIgnoreCase) == true) ?? 0;
            var totalNodes = info.NodeCount;
            var nodeStatus = readyNodes == totalNodes
                ? $"[green]{readyNodes}/{totalNodes} Ready[/]"
                : $"[yellow]{readyNodes}/{totalNodes} Ready[/]";
            Row("Nodes",        nodeStatus);

            if (info.Nodes?.Any() == true)
            {
                foreach (var node in info.Nodes)
                {
                    var readyMark = node.Ready?.Equals("True", StringComparison.OrdinalIgnoreCase) == true
                        ? "[green]●[/]" : "[red]●[/]";
                    grid.AddRow($"    [grey]{Markup.Escape(node.Name ?? ""),-16}[/]", readyMark);
                }
            }
        }

        Row("Namespaces",  namespaces is not null ? $"[cyan]{namespaces.Length}[/]" : "[grey]—[/]");

        if (!detail.IsLocal && detail.QueryParam is not null)
            Row("Query param",  $"[cyan]{Markup.Escape(detail.QueryParam)}[/]");

        var panel = new Panel(grid)
        {
            Header  = new PanelHeader($" Cluster: [bold]{Markup.Escape(detail.Name)}[/] "),
            Border  = BoxBorder.Rounded,
            Padding = new Padding(0, 1)
        };

        AnsiConsole.WriteLine();
        AnsiConsole.Write(panel);

        if (namespaces?.Length > 0)
        {
            AnsiConsole.WriteLine();
            AnsiConsole.MarkupLine($"[grey]Namespaces:[/] {string.Join(", ", namespaces.Select(n => $"[cyan]{Markup.Escape(n)}[/]"))}");
        }

        return 0;
    }

    record ClusterDetail(string Name, bool IsLocal, string? Server, string? QueryParam);
    record NodeInfo(string? Name, string? Ready);
    record ClusterInfoResponse(string? Version, int NodeCount, List<NodeInfo>? Nodes);
}

// ── cluster ping ───────────────────────────────────────────────────────────────
sealed class ClusterPingSettings : CommandSettings
{
    [CommandArgument(0, "<name>")]
    [Description("Name of the cluster to ping")]
    public string Name { get; init; } = "";

    [CommandOption("-n|--count")]
    [Description("Number of pings to send (default: 4)")]
    [DefaultValue(4)]
    public int Count { get; init; } = 4;
}

sealed class ClusterPingCommand : AsyncCommand<ClusterPingSettings>
{
    public override async Task<int> ExecuteAsync(CommandContext ctx, ClusterPingSettings s)
    {
        var cfg = KuberniqConfigManager.LoadOrFail();
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };

        // Verify cluster exists first
        var checkResp = await http.GetAsync($"{cfg.ServerUrl}/clusters/{s.Name}");
        if (checkResp.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            AnsiConsole.MarkupLine($"[red]✗[/] Cluster [bold]{s.Name}[/] is not registered.");
            AnsiConsole.MarkupLine("  Run [cyan]kuberniq cluster list[/] to see registered clusters.");
            return 1;
        }

        var isLocal = s.Name.Equals("local", StringComparison.OrdinalIgnoreCase);
        var clusterParam = isLocal ? "" : $"?cluster={s.Name}";
        var endpoint = $"{cfg.ServerUrl}/cluster/info{clusterParam}";

        AnsiConsole.MarkupLine($"Pinging cluster [bold]{s.Name}[/] via [grey]{cfg.ServerUrl}[/]...");
        AnsiConsole.WriteLine();

        var latencies = new List<long>();
        int failures = 0;

        for (int i = 1; i <= s.Count; i++)
        {
            var sw = System.Diagnostics.Stopwatch.StartNew();
            try
            {
                var resp = await http.GetAsync(endpoint);
                sw.Stop();
                if (resp.IsSuccessStatusCode)
                {
                    latencies.Add(sw.ElapsedMilliseconds);
                    AnsiConsole.MarkupLine(
                        $"  [grey]seq={i,2}[/]  [green]✓[/]  [cyan]{sw.ElapsedMilliseconds} ms[/]");
                }
                else
                {
                    sw.Stop();
                    failures++;
                    AnsiConsole.MarkupLine(
                        $"  [grey]seq={i,2}[/]  [red]✗[/]  HTTP {(int)resp.StatusCode}");
                }
            }
            catch (Exception ex)
            {
                sw.Stop();
                failures++;
                AnsiConsole.MarkupLine(
                    $"  [grey]seq={i,2}[/]  [red]✗[/]  {Markup.Escape(ex.Message)}");
            }

            if (i < s.Count)
                await Task.Delay(500);
        }

        AnsiConsole.WriteLine();

        if (latencies.Count == 0)
        {
            AnsiConsole.MarkupLine($"[red]✗[/] All {s.Count} pings failed. Cluster is unreachable.");
            return 1;
        }

        var min  = latencies.Min();
        var max  = latencies.Max();
        var avg  = (long)latencies.Average();
        var loss = (failures * 100) / s.Count;

        var table = new Table()
            .Border(TableBorder.Rounded)
            .AddColumn("Sent").AddColumn("Received").AddColumn("Lost")
            .AddColumn("Min").AddColumn("Avg").AddColumn("Max");

        table.AddRow(
            s.Count.ToString(),
            latencies.Count.ToString(),
            $"[{(loss > 0 ? "yellow" : "green")}]{loss}%[/]",
            $"[cyan]{min} ms[/]",
            $"[cyan]{avg} ms[/]",
            $"[cyan]{max} ms[/]");

        AnsiConsole.Write(table);
        return 0;
    }
}

// ── cluster set-default ────────────────────────────────────────────────────────
sealed class ClusterSetDefaultSettings : CommandSettings
{
    [CommandArgument(0, "<name>")]
    [Description("Name of the cluster to use as default (use 'local' to reset to in-cluster)")]
    public string Name { get; init; } = "";
}

sealed class ClusterSetDefaultCommand : AsyncCommand<ClusterSetDefaultSettings>
{
    public override async Task<int> ExecuteAsync(CommandContext ctx, ClusterSetDefaultSettings s)
    {
        var cfg = KuberniqConfigManager.LoadOrFail();
        using var http = new HttpClient();

        // Validate the cluster exists on the MCP server
        try
        {
            var resp = await http.GetAsync($"{cfg.ServerUrl}/clusters/{s.Name}");
            if (resp.StatusCode == System.Net.HttpStatusCode.NotFound)
            {
                AnsiConsole.MarkupLine($"[red]✗[/] Cluster [bold]{s.Name}[/] is not registered.");
                AnsiConsole.MarkupLine("  Run [cyan]kuberniq cluster list[/] to see registered clusters.");
                return 1;
            }
            resp.EnsureSuccessStatusCode();
        }
        catch (Exception ex)
        {
            AnsiConsole.MarkupLine($"[red]✗[/] Could not reach MCP server: {Markup.Escape(ex.Message)}");
            return 1;
        }

        var previous = cfg.DefaultCluster;
        KuberniqConfigManager.Save(cfg with { DefaultCluster = s.Name });

        if (previous is not null && !previous.Equals(s.Name, StringComparison.OrdinalIgnoreCase))
            AnsiConsole.MarkupLine($"[grey]Previous default:[/] {Markup.Escape(previous)}");

        AnsiConsole.MarkupLine($"[green]✓[/] Default cluster set to [bold]{Markup.Escape(s.Name)}[/].");
        AnsiConsole.MarkupLine($"  All cluster commands will now target [cyan]{Markup.Escape(s.Name)}[/] unless overridden.");
        AnsiConsole.MarkupLine($"  Reset anytime with [cyan]kuberniq cluster set-default local[/].");
        return 0;
    }
}

// ── cluster remove ─────────────────────────────────────────────────────────────
sealed class ClusterRemoveSettings : CommandSettings
{
    [CommandArgument(0, "<name>")]
    [Description("Name of the cluster to unregister")]
    public string Name { get; init; } = "";
}

sealed class ClusterRemoveCommand : AsyncCommand<ClusterRemoveSettings>
{
    public override async Task<int> ExecuteAsync(CommandContext ctx, ClusterRemoveSettings s)
    {
        var cfg = KuberniqConfigManager.LoadOrFail();

        if (!AnsiConsole.Confirm($"Remove cluster [bold]{s.Name}[/] from the MCP server?", defaultValue: false))
        {
            AnsiConsole.MarkupLine("[grey]Aborted.[/]");
            return 0;
        }

        try
        {
            using var http = new HttpClient();
            var resp = await http.DeleteAsync($"{cfg.ServerUrl}/clusters/{s.Name}");

            if (!resp.IsSuccessStatusCode)
            {
                var body = await resp.Content.ReadAsStringAsync();
                AnsiConsole.MarkupLine($"[red]✗[/] {(int)resp.StatusCode}: {Markup.Escape(body)}");
                return 1;
            }
        }
        catch (Exception ex)
        {
            AnsiConsole.MarkupLine($"[red]✗[/] {Markup.Escape(ex.Message)}");
            return 1;
        }

        AnsiConsole.MarkupLine($"[green]✓[/] Cluster [bold]{s.Name}[/] removed.");
        AnsiConsole.MarkupLine("  Note: the ServiceAccount and RBAC in the target cluster are [grey]not[/] deleted.");
        AnsiConsole.MarkupLine("  Delete them manually if no longer needed.");
        return 0;
    }
}
