using System.Net;
using KuberniqServer;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.TestHost;
using Xunit;

public class AdminOnlyFilterTests
{
    [Theory]
    [InlineData("POST", null, false)]
    [InlineData("POST", "viewer", false)]
    [InlineData("POST", "operator", false)]
    [InlineData("POST", "admin", true)]
    [InlineData("DELETE", null, false)]
    [InlineData("DELETE", "viewer", false)]
    [InlineData("DELETE", "operator", false)]
    [InlineData("DELETE", "admin", true)]
    public async Task OnlyAdminsCanInvokeMutation(string method, string? role, bool permitted)
    {
        var builder = WebApplication.CreateBuilder();
        builder.WebHost.UseTestServer();
        await using var app = builder.Build();
        app.Use(async (context, next) =>
        {
            // Both JWT and OIDC authentication populate this same role field.
            context.Items["role"] = role;
            await next(context);
        });
        var invoked = false;
        app.MapMethods("/clusters/test", [method], () => { invoked = true; return "ok"; })
            .AddEndpointFilter<AdminOnlyFilter>();
        await app.StartAsync();
        using var client = app.GetTestClient();
        using var response = await client.SendAsync(new HttpRequestMessage(new HttpMethod(method), "/clusters/test"));
        Assert.Equal(permitted ? HttpStatusCode.OK : HttpStatusCode.Forbidden, response.StatusCode);
        Assert.Equal(permitted, invoked);
    }
}
