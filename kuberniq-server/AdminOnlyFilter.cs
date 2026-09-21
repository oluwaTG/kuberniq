namespace KuberniqServer;

/// <summary>Require an administrator before invoking a cluster mutation handler.</summary>
public sealed class AdminOnlyFilter : IEndpointFilter
{
    public ValueTask<object?> InvokeAsync(EndpointFilterInvocationContext context, EndpointFilterDelegate next)
    {
        if (context.HttpContext.Items["role"]?.ToString() != "admin")
            return ValueTask.FromResult<object?>(Results.Json(new { error = "Forbidden." }, statusCode: 403));
        return next(context);
    }
}
