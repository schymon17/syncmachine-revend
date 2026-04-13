using System.Data;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Dapper;
using Microsoft.Data.Sqlite;
using MySqlConnector;
using Serilog;
using Serilog.Formatting.Compact;

var builder = WebApplication.CreateBuilder(args);

var serviceListenUrl = ResolveListenUrl(builder.Configuration["Service:ListenUrl"]);
var uiVersion = DateTime.UtcNow.ToString("yyyyMMddHHmmss");
builder.WebHost.UseUrls(serviceListenUrl);

builder.Host.UseSerilog((ctx, lc) => lc
	.MinimumLevel.Information()
	.Enrich.FromLogContext()
	.WriteTo.File(
		formatter: new CompactJsonFormatter(),
		path: "logs/revend-.log",
		rollingInterval: RollingInterval.Day,
		retainedFileCountLimit: 30,
		shared: true));

builder.Services.Configure<ServiceOptions>(builder.Configuration.GetSection("Service"));
builder.Services.AddSingleton<DbContextFactory>();
builder.Services.AddSingleton<EventStore>();
builder.Services.AddSingleton<AppSettingsFileStore>();
builder.Services.AddSingleton<ConfigStore>();
builder.Services.AddSingleton<ConfigSyncService>();
builder.Services.AddSingleton<SourceTransactionStore>();
builder.Services.AddHttpClient();
builder.Services.AddHostedService<ConfigSyncWorker>();

var app = builder.Build();

app.UseSerilogRequestLogging();
app.UseStaticFiles();

using (var scope = app.Services.CreateScope())
{
		var dbFactory = scope.ServiceProvider.GetRequiredService<DbContextFactory>();
		await DbBootstrap.InitializeAsync(dbFactory);

		var cfg = scope.ServiceProvider.GetRequiredService<IConfiguration>();
		var configStore = scope.ServiceProvider.GetRequiredService<ConfigStore>();
		var existingMachine = await configStore.GetMachineNumberAsync();
		var configuredMachine = cfg["Service:DefaultMachineId"];
		if (string.IsNullOrWhiteSpace(existingMachine) && !string.IsNullOrWhiteSpace(configuredMachine))
		{
				await configStore.SetMachineNumberAsync(configuredMachine.Trim());
		}
}

app.MapGet("/", () => Results.Redirect("/ui"));

app.MapGet("/health", () => Results.Ok(new { status = "ok", utc = DateTime.UtcNow }));
app.MapGet("/api/info", () => Results.Ok(new { listenUrl = serviceListenUrl, ui = $"{serviceListenUrl.TrimEnd('/')}/ui" }));

static string ResolveListenUrl(string? configured)
{
	const string fallbackUrl = "http://localhost:21011";
	if (string.IsNullOrWhiteSpace(configured))
	{
		return fallbackUrl;
	}

	if (!Uri.TryCreate(configured, UriKind.Absolute, out var uri))
	{
		return fallbackUrl;
	}

	return uri.IsDefaultPort || (uri.Port >= 1 && uri.Port <= 65535) ? configured : fallbackUrl;
}

app.MapGet("/api/setup/machine", async (ConfigStore configStore) =>
{
		var machineNumber = await configStore.GetMachineNumberAsync();
		return Results.Ok(new { machineNumber, isConfigured = !string.IsNullOrWhiteSpace(machineNumber) });
});

app.MapGet("/api/setup/storage-info", (AppSettingsFileStore fileStore) =>
{
		return Results.Ok(new
		{
			configFile = fileStore.FilePath,
			machineNumberPath = "Service:DefaultMachineId",
			runtimeConfigPath = "Service:RuntimeConfig"
		});
});

app.MapPost("/api/setup/machine", async (SetMachineRequest request, ConfigStore configStore, ConfigSyncService configSyncService) =>
{
		if (string.IsNullOrWhiteSpace(request.MachineNumber))
		{
				return Results.BadRequest(new { error = "Numer seryjny jest wymagany" });
		}

		var machineNumber = request.MachineNumber.Trim();
		await configStore.SetMachineNumberAsync(machineNumber);

		var syncResult = await configSyncService.SyncOnceAsync(machineNumber, CancellationToken.None);
		return Results.Ok(new
		{
				message = "Numer seryjny zapisany",
				machineNumber,
				configSync = new
				{
						syncResult.Success,
						syncResult.Skipped,
						syncResult.UpdatedCount,
						syncResult.Message
				}
		});
});

app.MapDelete("/api/setup/machine", async (ConfigStore configStore) =>
{
		await configStore.ClearMachineNumberAsync();
		return Results.Ok(new { message = "Numer seryjny wyczyszczony" });
});

app.MapGet("/api/monitor/events", async (int? take, EventStore store) =>
{
		var events = await store.GetLatestAsync(Math.Clamp(take ?? 100, 1, 500));
		return Results.Ok(events);
});

app.MapDelete("/api/monitor/events", async (EventStore store) =>
{
		await store.ClearAllAsync();
		return Results.Ok(new { message = "Historia zdarzeń została wyczyszczona" });
});

app.MapGet("/api/monitor/config", async (ConfigStore configStore) =>
{
		var all = await configStore.GetConfigRowsAsync();
		return Results.Ok(all);
});

app.MapPost("/api/monitor/config/reload", async (ConfigStore configStore, ConfigSyncService configSyncService) =>
{
		await configStore.ResetConfigurationStateAsync();
		return Results.Ok(new
		{
				success = true,
				message = "Konfiguracja została zresetowana. Wprowadź numer seryjny ponownie."
		});
});

app.MapGet("/api/monitor/modes", () => Results.Ok(new
{
		eventDriven = new[] { "transaction", "error", "bag", "reset" },
		scheduled = new[] { "config-sync-every-minute" }
}));

app.MapPost("/webhooks/transaction", async (JsonElement payload, IServiceProvider sp) =>
		await ProcessWebhookAsync("transaction", payload, sp));

app.MapPost("/webhooks/error", async (JsonElement payload, IServiceProvider sp) =>
		await ProcessWebhookAsync("error", payload, sp));

app.MapPost("/webhooks/bag", async (JsonElement payload, IServiceProvider sp) =>
		await ProcessWebhookAsync("bag", payload, sp));

app.MapPost("/webhooks/reset", async (JsonElement payload, IServiceProvider sp) =>
		await ProcessWebhookAsync("reset", payload, sp));

app.MapGet("/ui", async context =>
{
		context.Response.Headers.CacheControl = "no-store, no-cache, must-revalidate, max-age=0";
		context.Response.Headers.Pragma = "no-cache";
		context.Response.Headers.Expires = "0";
		context.Response.Headers["Clear-Site-Data"] = "\"cache\"";
		context.Response.ContentType = "text/html; charset=utf-8";
		await context.Response.WriteAsync(UiPage.Html.Replace("__UI_VERSION__", uiVersion));
});

app.Run();

static async Task<IResult> ProcessWebhookAsync(string eventType, JsonElement payload, IServiceProvider services)
{
		var store = services.GetRequiredService<EventStore>();
		var configStore = services.GetRequiredService<ConfigStore>();
		var options = services.GetRequiredService<IConfiguration>().GetSection("Service").Get<ServiceOptions>() ?? new ServiceOptions();
		var machineNumber = await configStore.GetMachineNumberAsync();

		if (string.IsNullOrWhiteSpace(machineNumber))
		{
				return Results.BadRequest(new { error = "Numer seryjny nie jest ustawiony. Najpierw wywołaj POST /api/setup/machine." });
		}

		if (eventType == "transaction")
		{
				var hasPrinterBarcode = payload.TryGetProperty("print_barcode", out _)
												|| payload.TryGetProperty("printer_barcode", out _)
																|| payload.TryGetProperty("printer_bracode", out _);
				if (!hasPrinterBarcode)
				{
						return Results.BadRequest(new { error = "Payload transakcji musi zawierać print_barcode (lub printer_barcode/printer_bracode)" });
				}
		}

		var payloadJson = payload.GetRawText();
		var request = await BuildForwardRequestAsync(eventType, payload, machineNumber, options, services);
		var forwardUrl = ResolveForwardUrl(eventType, options, request.EndpointPath);

		var entryId = await store.SaveIncomingAsync(eventType, payloadJson, machineNumber, forwardUrl);

		if (string.IsNullOrWhiteSpace(forwardUrl))
		{
				await store.MarkProcessedAsync(entryId, "skipped", null, "Brak skonfigurowanego URL przekazania dla tego typu zdarzenia", JsonSerializer.Serialize(request.Payload));
				return Results.Accepted($"/api/monitor/events", new { message = "Zapisano lokalnie (brak URL przekazania)", id = entryId });
		}

		try
		{
				var httpClientFactory = services.GetRequiredService<IHttpClientFactory>();
				var effectiveApi = ResolveEffectiveApi(options);
				using var client = httpClientFactory.CreateClient();
				if (!string.IsNullOrWhiteSpace(effectiveApi.Token))
				{
						client.DefaultRequestHeaders.Add("x-api-key-machine", effectiveApi.Token);
				}

				var content = new StringContent(JsonSerializer.Serialize(request.Payload), Encoding.UTF8, "application/json");
				var response = await client.PostAsync(forwardUrl, content);
				var responseBody = await response.Content.ReadAsStringAsync();

				await store.MarkProcessedAsync(
						entryId,
						response.IsSuccessStatusCode ? "forwarded" : "forward_failed",
						(int)response.StatusCode,
						responseBody,
						JsonSerializer.Serialize(request.Payload));

				if (!response.IsSuccessStatusCode)
				{
						return Results.BadRequest(new
						{
								error = "Przekazanie nie powiodło się",
								statusCode = (int)response.StatusCode,
								responseBody,
								id = entryId
						});
				}

				return Results.Ok(new { message = "Webhook odebrany i przekazany", id = entryId });
		}
		catch (Exception ex)
		{
				await store.MarkProcessedAsync(entryId, "exception", null, ex.Message, JsonSerializer.Serialize(request.Payload));
				return Results.Problem($"Wyjątek podczas przekazywania: {ex.Message}");
		}
}

static async Task<ForwardRequest> BuildForwardRequestAsync(string eventType, JsonElement incomingPayload, string machineId, ServiceOptions options, IServiceProvider services)
{
		var timestamp = DateTime.UtcNow.ToString("O");
		var incoming = JsonSerializer.Deserialize<object>(incomingPayload.GetRawText()) ?? new Dictionary<string, object?>();

		return eventType switch
		{
				"transaction" => await BuildTransactionRequestAsync(incomingPayload, incoming, machineId, timestamp, options, services),
				"error" => new ForwardRequest(
						string.Empty,
						new Dictionary<string, object?>
						{
								["machineId"] = machineId,
								["timestamp"] = timestamp,
								["kind"] = "status",
								["data"] = new Dictionary<string, object?>
								{
										["command"] = incoming
								}
						},
						"/status"),
				"bag" => new ForwardRequest(
						string.Empty,
						new Dictionary<string, object?>
						{
								["machineId"] = machineId,
								["integration"] = options.Integration,
								["timestamp"] = timestamp,
								["kind"] = "sync_bins",
								["data"] = new Dictionary<string, object?>
								{
										["empty_records"] = new[] { incoming }
								}
						},
						"/bins"),
				"reset" => new ForwardRequest(
						string.Empty,
						new Dictionary<string, object?>
						{
								["mid"] = machineId,
								["timestamp"] = timestamp
						},
						"/reset"),
				_ => new ForwardRequest(string.Empty, incoming, string.Empty)
		};
}

static async Task<ForwardRequest> BuildTransactionRequestAsync(JsonElement incomingPayload, object incomingObject, string machineId, string timestamp, ServiceOptions options, IServiceProvider services)
{
		var transactionId = GetPrinterBarcode(incomingPayload);
		var details = new List<Dictionary<string, object?>>();
		var lastTransactionTime = ResolveTransactionTime(incomingPayload, timestamp);

		if (!string.IsNullOrWhiteSpace(transactionId))
		{
				try
				{
						var sourceStore = services.GetRequiredService<SourceTransactionStore>();
						var dbRows = await sourceStore.GetTransactionRowsByPrintBarcodeAsync(transactionId, options.SourceDb);
						if (dbRows.Count > 0)
						{
								details = dbRows;
								var maxDateline = dbRows
										.Select(x => x.TryGetValue("dateline", out var value) ? value : null)
										.Select(ExtractUnixTimestamp)
										.Max();
								if (maxDateline > 0)
								{
										lastTransactionTime = DateTimeOffset.FromUnixTimeSeconds(maxDateline).UtcDateTime.ToString("yyyy-MM-dd HH:mm:ss");
								}
						}
				}
				catch
				{
						// If source DB is unavailable, keep fallback to incoming webhook payload.
				}
		}

		if (details.Count == 0)
		{
				details.Add(new Dictionary<string, object?>
				{
						["print_barcode"] = transactionId,
						["webhook"] = incomingObject
				});
		}

		var payload = new Dictionary<string, object?>
		{
				["machineId"] = machineId,
				["timestamp"] = timestamp,
				["kind"] = "transactions",
				["data"] = new Dictionary<string, object?>
				{
						["transactions"] = new Dictionary<string, object?>
						{
								[transactionId] = new Dictionary<string, object?>
								{
										["details"] = details,
										["last_transaction_time"] = lastTransactionTime
								}
						},
						["mid"] = machineId
				},
				["integration"] = options.Integration ?? "polka"
		};

		return new ForwardRequest(string.Empty, payload, "/trans");
}

static long ExtractUnixTimestamp(object? value)
{
		if (value is null || value is DBNull)
		{
				return 0;
		}

		return value switch
		{
				long l => l,
				int i => i,
				decimal d => (long)d,
				double db => (long)db,
				float f => (long)f,
				string s when long.TryParse(s, out var parsed) => parsed,
				_ => 0
		};
}

static string? ResolveForwardUrl(string eventType, ServiceOptions options, string endpointPath)
{
		var effectiveApi = ResolveEffectiveApi(options);
		var explicitUrl = eventType switch
		{
				"transaction" => options.Forwarding?.TransactionUrl,
				"error" => options.Forwarding?.ErrorUrl,
				"bag" => options.Forwarding?.BagUrl,
				"reset" => options.Forwarding?.ResetUrl,
				_ => null
		};

		if (!string.IsNullOrWhiteSpace(explicitUrl))
		{
				return explicitUrl;
		}

		if (string.IsNullOrWhiteSpace(effectiveApi.BaseUrl) || string.IsNullOrWhiteSpace(endpointPath))
		{
				return null;
		}

		return $"{effectiveApi.BaseUrl.TrimEnd('/')}/{endpointPath.TrimStart('/')}";
}

static ApiOptions ResolveEffectiveApi(ServiceOptions options)
{
		return new ApiOptions
		{
				BaseUrl = !string.IsNullOrWhiteSpace(options.Api?.BaseUrl)
						? options.Api?.BaseUrl
						: options.Legacy?.Api?.BaseUrl,
				Token = !string.IsNullOrWhiteSpace(options.Api?.Token)
						? options.Api?.Token
						: options.Legacy?.Api?.Token
		};
}

static string GetPrinterBarcode(JsonElement payload)
{
		if (payload.TryGetProperty("print_barcode", out var print) && print.ValueKind == JsonValueKind.String)
		{
				return print.GetString() ?? string.Empty;
		}

		if (payload.TryGetProperty("printer_barcode", out var correct) && correct.ValueKind == JsonValueKind.String)
		{
				return correct.GetString() ?? string.Empty;
		}

		if (payload.TryGetProperty("printer_bracode", out var typo) && typo.ValueKind == JsonValueKind.String)
		{
				return typo.GetString() ?? string.Empty;
		}

		return "unknown";
}

static string ResolveTransactionTime(JsonElement payload, string fallbackIso)
{
		if (payload.TryGetProperty("datetime", out var datetime) && datetime.ValueKind == JsonValueKind.String)
		{
				return datetime.GetString() ?? fallbackIso;
		}

		if (payload.TryGetProperty("dateline", out var dateline))
		{
				if (dateline.ValueKind == JsonValueKind.Number && dateline.TryGetInt64(out var unix))
				{
						return DateTimeOffset.FromUnixTimeSeconds(unix).UtcDateTime.ToString("yyyy-MM-dd HH:mm:ss");
				}
				if (dateline.ValueKind == JsonValueKind.String && long.TryParse(dateline.GetString(), out var unixFromString))
				{
						return DateTimeOffset.FromUnixTimeSeconds(unixFromString).UtcDateTime.ToString("yyyy-MM-dd HH:mm:ss");
				}
		}

		return fallbackIso;
}

internal sealed record ForwardRequest(string Url, object Payload, string EndpointPath);

internal sealed class DbContextFactory
{
		private readonly IConfiguration _configuration;

		public DbContextFactory(IConfiguration configuration)
		{
				_configuration = configuration;
		}

		public IDbConnection Create()
		{
				var connString = _configuration.GetConnectionString("Default") ?? "Data Source=revened-service.db";
				return new SqliteConnection(connString);
		}

		public string GetDatabaseLocation()
		{
				var connString = _configuration.GetConnectionString("Default") ?? "Data Source=revened-service.db";
				var builder = new SqliteConnectionStringBuilder(connString);
				var dataSource = builder.DataSource;
				if (Path.IsPathRooted(dataSource))
				{
						return dataSource;
				}

				return Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, dataSource));
		}
}

internal static class DbBootstrap
{
		public static async Task InitializeAsync(DbContextFactory dbFactory)
		{
				await using var connection = (SqliteConnection)dbFactory.Create();
				await connection.OpenAsync();

				var sql = @"
CREATE TABLE IF NOT EXISTS machine_settings (
		id INTEGER PRIMARY KEY CHECK (id = 1),
		machine_number TEXT NOT NULL,
		updated_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		event_type TEXT NOT NULL,
		incoming_payload TEXT NOT NULL,
		outgoing_payload TEXT,
		machine_number TEXT NOT NULL,
		forward_url TEXT,
		status TEXT NOT NULL,
		response_code INTEGER,
		response_body TEXT,
		created_utc TEXT NOT NULL,
		updated_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
		key TEXT PRIMARY KEY,
		value TEXT NOT NULL,
		updated_utc TEXT NOT NULL
);
";

				await connection.ExecuteAsync(sql);

				try
				{
						await connection.ExecuteAsync("ALTER TABLE events ADD COLUMN outgoing_payload TEXT;");
				}
				catch
				{
						// Column already exists on upgraded databases.
				}
		}
}

internal sealed class EventStore
{
		private readonly DbContextFactory _dbFactory;

		public EventStore(DbContextFactory dbFactory)
		{
				_dbFactory = dbFactory;
		}

		public async Task<long> SaveIncomingAsync(string eventType, string payload, string machineNumber, string? forwardUrl)
		{
				await using var connection = (SqliteConnection)_dbFactory.Create();
				await connection.OpenAsync();

				const string sql = @"
INSERT INTO events (event_type, incoming_payload, machine_number, forward_url, status, created_utc, updated_utc)
VALUES (@EventType, @IncomingPayload, @MachineNumber, @ForwardUrl, @Status, @Utc, @Utc);
SELECT last_insert_rowid();";

				return await connection.ExecuteScalarAsync<long>(sql, new
				{
						EventType = eventType,
						IncomingPayload = payload,
						MachineNumber = machineNumber,
						ForwardUrl = forwardUrl,
						Status = "received",
						Utc = DateTime.UtcNow.ToString("O")
				});
		}

		public async Task MarkProcessedAsync(long id, string status, int? responseCode, string? responseBody, string? outgoingPayload)
		{
				await using var connection = (SqliteConnection)_dbFactory.Create();
				await connection.OpenAsync();

				const string sql = @"
UPDATE events
SET status = @Status,
		response_code = @ResponseCode,
		response_body = @ResponseBody,
		outgoing_payload = @OutgoingPayload,
		updated_utc = @Utc
WHERE id = @Id;";

				await connection.ExecuteAsync(sql, new
				{
						Id = id,
						Status = status,
						ResponseCode = responseCode,
						ResponseBody = responseBody,
						OutgoingPayload = outgoingPayload,
						Utc = DateTime.UtcNow.ToString("O")
				});
		}

		public async Task ClearAllAsync()
		{
				await using var connection = (SqliteConnection)_dbFactory.Create();
				await connection.OpenAsync();
				await connection.ExecuteAsync("DELETE FROM events;");
		}

		public async Task<IReadOnlyList<EventRow>> GetLatestAsync(int take)
		{
				await using var connection = (SqliteConnection)_dbFactory.Create();
				await connection.OpenAsync();

				const string sql = @"
SELECT id,
			 event_type AS EventType,
			 incoming_payload AS IncomingPayload,
			 outgoing_payload AS OutgoingPayload,
			 machine_number AS MachineNumber,
			 forward_url AS ForwardUrl,
			 status,
			 response_code AS ResponseCode,
			 response_body AS ResponseBody,
			 created_utc AS CreatedUtc,
			 updated_utc AS UpdatedUtc
FROM events
ORDER BY id DESC
LIMIT @Take;";

				var rows = await connection.QueryAsync<EventRow>(sql, new { Take = take });
				return rows.ToList();
		}
}

internal sealed class ConfigStore
{
		private readonly AppSettingsFileStore _settingsFile;

		public ConfigStore(AppSettingsFileStore settingsFile)
		{
				_settingsFile = settingsFile;
		}

		public async Task<string?> GetMachineNumberAsync()
		{
				var root = await _settingsFile.ReadAsync();
				var machine = root["Service"]?["DefaultMachineId"]?.GetValue<string>();
				return string.IsNullOrWhiteSpace(machine) ? null : machine;
		}

		public async Task SetMachineNumberAsync(string machineNumber)
		{
				await _settingsFile.UpdateAsync(root =>
				{
						var service = EnsureSection(root, "Service");
						service["DefaultMachineId"] = machineNumber;
				});
		}

		public async Task ClearMachineNumberAsync()
		{
				await _settingsFile.UpdateAsync(root =>
				{
						var service = EnsureSection(root, "Service");
						service["DefaultMachineId"] = string.Empty;
				});
		}

		public async Task UpsertManyAsync(Dictionary<string, string> values)
		{
				await _settingsFile.UpdateAsync(root =>
				{
						var service = EnsureSection(root, "Service");
						var runtime = service["RuntimeConfig"] as JsonObject ?? new JsonObject();
						service["RuntimeConfig"] = runtime;

						var utc = DateTime.UtcNow.ToString("O");
						foreach (var kv in values)
						{
							runtime[kv.Key] = new JsonObject
							{
									["value"] = kv.Value,
									["updated_utc"] = utc
							};
						}
				});
		}

		public async Task<IReadOnlyList<ConfigRow>> GetConfigRowsAsync()
		{
				var root = await _settingsFile.ReadAsync();
				var runtime = root["Service"]?["RuntimeConfig"] as JsonObject;
				if (runtime is null)
				{
						return Array.Empty<ConfigRow>();
				}

				var rows = new List<ConfigRow>();
				foreach (var prop in runtime)
				{
						if (prop.Value is JsonObject node)
						{
								rows.Add(new ConfigRow
								{
										Key = prop.Key,
										Value = NodeToString(node["value"]),
										UpdatedUtc = node["updated_utc"]?.GetValue<string>() ?? string.Empty
								});
						}
						else
						{
								rows.Add(new ConfigRow
								{
										Key = prop.Key,
										Value = NodeToString(prop.Value),
										UpdatedUtc = string.Empty
								});
						}
				}

				return rows.OrderBy(x => x.Key, StringComparer.OrdinalIgnoreCase).ToList();
		}

		public async Task ResetConfigurationStateAsync()
		{
				await _settingsFile.UpdateAsync(root =>
				{
						var service = EnsureSection(root, "Service");
						service["DefaultMachineId"] = string.Empty;
						service["RuntimeConfig"] = new JsonObject();
				});
		}

		private static JsonObject EnsureSection(JsonObject root, string name)
		{
				var section = root[name] as JsonObject;
				if (section is not null)
				{
						return section;
				}

				section = new JsonObject();
				root[name] = section;
				return section;
		}

		private static string NodeToString(JsonNode? node)
		{
				if (node is null)
				{
						return string.Empty;
				}

				if (node is JsonValue value)
				{
						if (value.TryGetValue<string>(out var str))
						{
								return str;
						}

						return value.ToJsonString();
				}

				return node.ToJsonString();
		}
}

internal sealed class AppSettingsFileStore
{
		private readonly SemaphoreSlim _writeLock = new(1, 1);
		public string FilePath { get; }

		public AppSettingsFileStore(IHostEnvironment environment)
		{
				FilePath = Path.Combine(environment.ContentRootPath, "appsettings.json");
		}

		public async Task<JsonObject> ReadAsync()
		{
				if (!File.Exists(FilePath))
				{
						return new JsonObject();
				}

				var text = await File.ReadAllTextAsync(FilePath);
				if (string.IsNullOrWhiteSpace(text))
				{
						return new JsonObject();
				}

				return JsonNode.Parse(text) as JsonObject ?? new JsonObject();
		}

		public async Task UpdateAsync(Action<JsonObject> apply)
		{
				await _writeLock.WaitAsync();
				try
				{
						var root = await ReadAsync();
						apply(root);
						var output = root.ToJsonString(new JsonSerializerOptions { WriteIndented = true });
						await File.WriteAllTextAsync(FilePath, output + Environment.NewLine);
				}
				finally
				{
						_writeLock.Release();
				}
		}
}

internal sealed record ConfigSyncResult(bool Success, bool Skipped, int UpdatedCount, string Message);

internal sealed class ConfigSyncService
{
		private readonly ConfigStore _configStore;
		private readonly IHttpClientFactory _httpClientFactory;
		private readonly IConfiguration _configuration;
		private readonly ILogger<ConfigSyncService> _logger;

		public ConfigSyncService(
				ConfigStore configStore,
				IHttpClientFactory httpClientFactory,
				IConfiguration configuration,
				ILogger<ConfigSyncService> logger)
		{
				_configStore = configStore;
				_httpClientFactory = httpClientFactory;
				_configuration = configuration;
				_logger = logger;
		}

		public async Task<ConfigSyncResult> SyncOnceAsync(string? machineIdOverride, CancellationToken cancellationToken)
		{
				var options = _configuration.GetSection("Service").Get<ServiceOptions>() ?? new ServiceOptions();
				var url = options.MainConfigUrl;
				if (string.IsNullOrWhiteSpace(url))
				{
						return new ConfigSyncResult(false, true, 0, "MainConfigUrl is empty");
				}

				var machineId = string.IsNullOrWhiteSpace(machineIdOverride)
						? await _configStore.GetMachineNumberAsync()
						: machineIdOverride;
				if (string.IsNullOrWhiteSpace(machineId))
				{
						return new ConfigSyncResult(false, true, 0, "Machine number not configured");
				}

				try
				{
						using var client = _httpClientFactory.CreateClient();
						if (!string.IsNullOrWhiteSpace(options.Api?.Token))
						{
								client.DefaultRequestHeaders.Add("x-api-key-machine", options.Api.Token);
						}

						var bodyPayload = new
						{
								machineId,
								mid = machineId,
								timestamp = DateTime.UtcNow.ToString("O")
						};

						using var response = await client.PostAsync(
								url,
								new StringContent(JsonSerializer.Serialize(bodyPayload), Encoding.UTF8, "application/json"),
								cancellationToken);

						var body = await response.Content.ReadAsStringAsync(cancellationToken);
						if (!response.IsSuccessStatusCode)
						{
								return new ConfigSyncResult(false, false, 0, $"Config sync returned status {(int)response.StatusCode}");
						}

						var values = ParseToDictionary(body);
						if (values.Count == 0)
						{
								return new ConfigSyncResult(false, true, 0, "Config response has no key/value pairs");
						}

						await _configStore.UpsertManyAsync(values);
						return new ConfigSyncResult(true, false, values.Count, "Config synced");
				}
				catch (Exception ex)
				{
						_logger.LogError(ex, "Config sync failed");
						return new ConfigSyncResult(false, false, 0, $"Config sync failed: {ex.Message}");
				}
		}

		private static Dictionary<string, string> ParseToDictionary(string body)
		{
				var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

				using var doc = JsonDocument.Parse(body);
				if (doc.RootElement.ValueKind == JsonValueKind.Object)
				{
						var source = doc.RootElement;
						if (source.TryGetProperty("data", out var dataNode)
								&& dataNode.ValueKind == JsonValueKind.Object
								&& dataNode.TryGetProperty("attributes", out var attrsNode)
								&& attrsNode.ValueKind == JsonValueKind.Object)
						{
								source = attrsNode;
						}
						else if (source.TryGetProperty("attributes", out var directAttrs) && directAttrs.ValueKind == JsonValueKind.Object)
						{
								source = directAttrs;
						}

						foreach (var prop in source.EnumerateObject())
						{
								result[prop.Name] = prop.Value.ValueKind == JsonValueKind.String
										? prop.Value.GetString() ?? string.Empty
										: prop.Value.GetRawText();
						}
				}

				return result;
		}
}

internal sealed class SourceTransactionStore
{
		public async Task<List<Dictionary<string, object?>>> GetTransactionRowsByPrintBarcodeAsync(string printBarcode, SourceDbOptions? db)
		{
				if (db is null
						|| string.IsNullOrWhiteSpace(db.Host)
						|| string.IsNullOrWhiteSpace(db.Database)
						|| string.IsNullOrWhiteSpace(db.Username)
						|| string.IsNullOrWhiteSpace(db.Password))
				{
						return new List<Dictionary<string, object?>>();
				}

				var table = string.IsNullOrWhiteSpace(db.TransactionTable) ? "user_transaction" : db.TransactionTable;
				var csBuilder = new MySqlConnectionStringBuilder
				{
						Server = db.Host,
						Port = (uint)(db.Port <= 0 ? 3306 : db.Port),
						Database = db.Database,
						UserID = db.Username,
						Password = db.Password,
						SslMode = MySqlSslMode.None,
						AllowUserVariables = true,
						DefaultCommandTimeout = 15,
						ConnectionTimeout = 10
				};

				await using var conn = new MySqlConnection(csBuilder.ConnectionString);
				await conn.OpenAsync();

				var sql = $"SELECT * FROM `{table}` WHERE print_barcode = @PrintBarcode ORDER BY dateline ASC";
				var rows = await conn.QueryAsync(sql, new { PrintBarcode = printBarcode });
				var list = new List<Dictionary<string, object?>>();

				foreach (var row in rows)
				{
						if (row is not IDictionary<string, object> map)
						{
								continue;
						}

						var item = new Dictionary<string, object?>(StringComparer.OrdinalIgnoreCase);
						foreach (var kv in map)
						{
								item[kv.Key] = kv.Value is DBNull ? null : kv.Value;
						}
						list.Add(item);
				}

				return list;
		}
}

internal sealed class ConfigSyncWorker : BackgroundService
{
		private readonly ConfigSyncService _configSyncService;
		private readonly ILogger<ConfigSyncWorker> _logger;

		public ConfigSyncWorker(
				ConfigSyncService configSyncService,
				ILogger<ConfigSyncWorker> logger)
		{
				_configSyncService = configSyncService;
				_logger = logger;
		}

		protected override async Task ExecuteAsync(CancellationToken stoppingToken)
		{
				using var timer = new PeriodicTimer(TimeSpan.FromMinutes(1));

				await SyncOnceAsync(stoppingToken);

				while (!stoppingToken.IsCancellationRequested && await timer.WaitForNextTickAsync(stoppingToken))
				{
						await SyncOnceAsync(stoppingToken);
				}
		}

		private async Task SyncOnceAsync(CancellationToken cancellationToken)
		{
				var result = await _configSyncService.SyncOnceAsync(null, cancellationToken);
				if (result.Success)
				{
						_logger.LogInformation("Config sync updated {Count} rows", result.UpdatedCount);
						return;
				}

				if (!result.Skipped)
				{
						_logger.LogWarning("Config sync not completed: {Message}", result.Message);
				}
		}
}

internal sealed record SetMachineRequest(string MachineNumber);

internal sealed class ServiceOptions
{
		public string? MainConfigUrl { get; init; }
		public string? DefaultMachineId { get; init; }
		public string? Integration { get; init; }
		public SourceDbOptions? SourceDb { get; init; }
		public ApiOptions? Api { get; init; }
		public LegacyOptions? Legacy { get; init; }
		public ForwardingOptions? Forwarding { get; init; }
}

internal sealed class SourceDbOptions
{
		public string? Host { get; init; }
		public int Port { get; init; } = 3306;
		public string? Database { get; init; }
		public string? Username { get; init; }
		public string? Password { get; init; }
		public string? TransactionTable { get; init; } = "user_transaction";
}

internal sealed class ApiOptions
{
		public string? BaseUrl { get; init; }
		public string? Token { get; init; }
}

internal sealed class LegacyOptions
{
		public string? MachineId { get; init; }
		public LegacyDbOptions? Db { get; init; }
		public ApiOptions? Api { get; init; }
}

internal sealed class LegacyDbOptions
{
		public string? Driver { get; init; }
		public string? Host { get; init; }
		public int? Port { get; init; }
		public string? Database { get; init; }
		public string? Username { get; init; }
		public string? Password { get; init; }
}

internal sealed class ForwardingOptions
{
		public string? TransactionUrl { get; init; }
		public string? ErrorUrl { get; init; }
		public string? BagUrl { get; init; }
		public string? ResetUrl { get; init; }
}

internal sealed class EventRow
{
		public long Id { get; init; }
		public string EventType { get; init; } = string.Empty;
		public string IncomingPayload { get; init; } = string.Empty;
		public string? OutgoingPayload { get; init; }
		public string MachineNumber { get; init; } = string.Empty;
		public string? ForwardUrl { get; init; }
		public string Status { get; init; } = string.Empty;
		public int? ResponseCode { get; init; }
		public string? ResponseBody { get; init; }
		public string CreatedUtc { get; init; } = string.Empty;
		public string UpdatedUtc { get; init; } = string.Empty;
}

internal sealed class ConfigRow
{
		public string Key { get; init; } = string.Empty;
		public string Value { get; init; } = string.Empty;
		public string UpdatedUtc { get; init; } = string.Empty;
}

internal static class UiPage
{
		public const string Html = """
<!DOCTYPE html>
<html lang="en">
<head>
	<meta charset="UTF-8" />
	<meta name="viewport" content="width=device-width, initial-scale=1.0" />
	<title>Revend Event Monitor</title>
	<style>
		:root {
			--bg: #0f1418;
			--bg-soft: #151c22;
			--card: #1b242c;
			--ink: #e6eef3;
			--muted: #9eb0bb;
			--accent: #16a57a;
			--accent-strong: #128662;
			--sun: #d1a837;
			--warn: #ef5350;
			--ok: #32c48d;
			--line: #2c3a45;
		}

		* { box-sizing: border-box; }

		body {
			margin: 0;
			font-family: "Segoe UI", "Avenir Next", sans-serif;
			color: var(--ink);
			background:
				radial-gradient(130% 42% at 50% -10%, rgba(50, 124, 209, 0.20) 0%, rgba(50, 124, 209, 0) 60%),
				radial-gradient(120% 38% at 18% 104%, rgba(30, 167, 136, 0.12) 0%, rgba(30, 167, 136, 0) 65%),
				linear-gradient(180deg, #eff4f9 0%, #e6edf5 48%, #dde6f0 100%);
			min-height: 100vh;
		}

		.wrap {
			max-width: 1380px;
			margin: 0 auto;
			padding: 20px;
			animation: fadeIn 0.4s ease;
		}

		h1 {
			margin-top: 0;
			letter-spacing: 0.04em;
			font-size: clamp(1.4rem, 3vw, 2rem);
			color: #1d4f91;
			text-transform: uppercase;
			text-align: center;
		}

		body.setup-mode h1 {
			display: none;
		}

		.hero {
			display: flex;
			justify-content: space-between;
			gap: 12px;
			align-items: center;
			padding: 14px 16px;
			border: 1px solid #2c3a45;
			border-radius: 14px;
			background: linear-gradient(135deg, #162129, #10161b 52%, #1d1a11);
			margin-bottom: 16px;
		}

		.hero-machine {
			display: inline-flex;
			align-items: center;
			gap: 8px;
			padding: 7px 12px;
			border-radius: 999px;
			background: #10181f;
			border: 1px solid #2a3944;
			font-size: 12px;
			color: #d5e5ef;
		}

		.hero-machine strong {
			color: #f5fffb;
			font-size: 12px;
		}

		.grid {
			display: grid;
			gap: 16px;
			grid-template-columns: repeat(12, 1fr);
		}

		body.setup-mode .grid {
			display: flex;
			align-items: center;
			justify-content: center;
			min-height: calc(100vh - 120px);
		}

		.card {
			background: var(--card);
			border: 1px solid var(--line);
			border-radius: 14px;
			padding: 16px;
			box-shadow: 0 10px 24px rgba(0, 0, 0, 0.35);
		}

		.card-head {
			display: flex;
			align-items: center;
			justify-content: space-between;
			gap: 10px;
			margin-bottom: 10px;
		}

		.card-head h2 {
			margin: 0;
		}

		.btn-secondary {
			padding: 8px 12px;
			font-size: 13px;
			border-radius: 6px;
			border: 1px solid #94a3b8;
			background: #eef2f7;
			color: #1f2937;
			cursor: pointer;
		}

		.btn-secondary:hover {
			background: #e2e8f0;
		}

		.setup { grid-column: span 12; }
		.config { grid-column: span 4; }
		.events { grid-column: span 8; }

		@media (max-width: 900px) {
			.config, .events { grid-column: span 12; }
		}

		input, button {
			padding: 10px 12px;
			border-radius: 10px;
			border: 1px solid #33424d;
			font-size: 14px;
		}

		input {
			background: #0f151b;
			color: var(--ink);
		}

		button {
			background: var(--accent);
			color: white;
			border: none;
			cursor: pointer;
			font-weight: 600;
		}

		button:hover { background: var(--accent-strong); }

		.row {
			display: flex;
			gap: 10px;
			flex-wrap: wrap;
			align-items: center;
		}

		.hidden {
			display: none !important;
		}

		.setup-screen {
			grid-column: span 12;
			max-width: 460px;
			margin: 20px;
			padding: 20px;
			border: 1px solid #d0d7de;
			background: linear-gradient(180deg, #f9fbfd 0%, #f2f5f8 100%);
			box-shadow: 0 10px 28px rgba(0, 0, 0, 0.22);
			font-family: "Segoe UI", Tahoma, sans-serif;
		}

		body.setup-mode .setup-screen {
			width: min(460px, calc(100% - 40px));
			margin: 20px;
		}

		.setup-logo-wrap {
			display: flex;
			justify-content: center;
			margin-bottom: 14px;
		}

		.setup-logo {
			height: 46px;
			width: auto;
		}

		.setup-screen h2 {
			font-size: 1.7rem;
			margin-bottom: 14px;
			text-transform: none;
			letter-spacing: 0.01em;
			text-align: center;
			color: #1f2937;
			text-shadow: none;
		}

		.setup-screen p {
			margin-top: 0;
			text-align: center;
			color: #4b5563;
			font-size: 1rem;
		}

		.setup-screen .row {
			display: flex;
			flex-direction: column;
			align-items: center;
			width: 100%;
			gap: 12px;
		}

		.touch-input {
			min-height: 52px;
			font-size: 1.15rem;
			padding: 8px 12px;
			width: 100%;
			max-width: 560px;
			border: 1px solid #bcc5d0;
			background: #ffffff;
			color: #111827;
			border-radius: 6px;
		}

		.touch-button {
			min-height: 52px;
			font-size: 1rem;
			padding: 0 14px;
			width: 100%;
			max-width: 560px;
			text-transform: none;
			letter-spacing: 0;
			border-radius: 6px;
			border: 1px solid #0f5fb8;
			background: linear-gradient(180deg, #2f8df3 0%, #1d72cd 100%);
			color: #ffffff;
			box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.25);
		}

		.touch-button:hover {
			background: linear-gradient(180deg, #3f98f8 0%, #2a7fdc 100%);
		}

		.window-author {
			position: fixed;
			right: 16px;
			bottom: 10px;
			font-size: 12px;
			color: #64748b;
			font-weight: 600;
			z-index: 20;
		}

		.easter-overlay {
			position: fixed;
			inset: 0;
			opacity: 0;
			pointer-events: none;
			transition: opacity 160ms linear;
			z-index: 9999;
		}

		.easter-overlay.show {
			opacity: 1;
		}

		#easterCanvas {
			width: 100%;
			height: 100%;
			display: block;
		}

		.easter-message {
			position: absolute;
			left: 50%;
			top: 45%;
			transform: translate(-50%, -50%);
			font-family: "Courier New", monospace;
			font-size: clamp(18px, 2.8vw, 34px);
			font-weight: 700;
			letter-spacing: 0.08em;
			text-transform: uppercase;
			color: #7cff95;
			text-shadow:
				0 0 10px rgba(76, 255, 120, 0.95),
				0 0 22px rgba(76, 255, 120, 0.65),
				0 0 38px rgba(76, 255, 120, 0.4);
		}

		.easter-overlay.sad {
			background: rgba(9, 15, 28, 0.78);
		}

		.easter-overlay.sad .easter-message {
			color: #9fb3c8;
			text-shadow:
				0 0 10px rgba(159, 179, 200, 0.6),
				0 0 24px rgba(159, 179, 200, 0.24);
		}

		.modal-overlay {
			position: fixed;
			inset: 0;
			display: none;
			align-items: center;
			justify-content: center;
			background: rgba(15, 23, 42, 0.55);
			backdrop-filter: blur(1px);
			z-index: 10000;
		}

		.modal-overlay.show {
			display: flex;
		}

		.modal-card {
			width: min(460px, calc(100% - 32px));
			background: #f8fafc;
			border: 1px solid #cbd5e1;
			border-radius: 12px;
			box-shadow: 0 20px 44px rgba(2, 8, 23, 0.28);
			padding: 16px;
		}

		.modal-title {
			margin: 0 0 8px;
			font-size: 18px;
			color: #0f172a;
		}

		.modal-text {
			margin: 0 0 16px;
			color: #334155;
			line-height: 1.45;
		}

		.modal-actions {
			display: flex;
			justify-content: flex-end;
			gap: 8px;
		}

		.modal-btn {
			padding: 8px 12px;
			border-radius: 8px;
			border: 1px solid #94a3b8;
			font-size: 13px;
			cursor: pointer;
		}

		.modal-btn.cancel {
			background: #eef2f7;
			color: #1f2937;
		}

		.modal-btn.confirm {
			background: #1d72cd;
			border-color: #1d72cd;
			color: #ffffff;
		}

		#saveMachine[disabled] {
			opacity: 0.65;
			cursor: progress;
		}

		.setup-feedback {
			margin-top: 10px;
			min-height: 20px;
			font-size: 13px;
			color: var(--muted);
		}

		.msg-ok { color: var(--ok); }
		.msg-warn { color: var(--sun); }
		.msg-err { color: var(--warn); }

		h2 {
			margin-top: 0;
			font-size: 1.1rem;
			letter-spacing: 0.01em;
		}

		table {
			width: 100%;
			border-collapse: collapse;
			font-size: 13px;
		}

		th, td {
			text-align: left;
			border-bottom: 1px solid var(--line);
			padding: 8px 6px;
			vertical-align: top;
			word-break: break-word;
		}

		th {
			color: var(--muted);
			font-weight: 700;
			position: sticky;
			top: 0;
			background: #1f2b33;
			z-index: 2;
		}

		tbody tr:nth-child(even) {
			background: #1a232b;
		}

		code {
			font-family: "SF Mono", Menlo, monospace;
			font-size: 12px;
			background: #131b22;
			padding: 2px 4px;
			border-radius: 4px;
			color: #d7e7f0;
		}

		.table-wrap {
			max-height: 68vh;
			overflow: auto;
			border: 1px solid var(--line);
			border-radius: 12px;
			background: #172028;
		}

		.badge {
			display: inline-block;
			padding: 3px 8px;
			border-radius: 999px;
			font-size: 12px;
			font-weight: 700;
			line-height: 1.4;
		}

		.badge-ok {
			color: #0f2f22;
			background: #32c48d;
		}

		.badge-warn {
			color: #3b2c02;
			background: #d1a837;
		}

		.badge-err {
			color: #4c0d0a;
			background: #ef5350;
		}

		.json-block {
			display: block;
			margin: 0;
			padding: 8px;
			border-radius: 8px;
			background: #10181f;
			border: 1px solid #2a3944;
			max-height: 170px;
			overflow: auto;
			white-space: pre-wrap;
			font-family: "SF Mono", Menlo, monospace;
			font-size: 12px;
			line-height: 1.35;
			color: #d5e5ef;
		}

		.info {
			grid-column: span 12;
			background: linear-gradient(90deg, #ffffff, #f4fbf8);
		}

		.info p {
			margin: 6px 0;
		}

		@keyframes fadeIn {
			from { opacity: 0; transform: translateY(6px); }
			to { opacity: 1; transform: translateY(0); }
		}

		@media (max-width: 900px) {
			.wrap { padding: 12px; }
			table { font-size: 12px; }
			.json-block { max-height: 130px; }
			.setup-screen {
				margin: 8px auto;
				padding: 16px;
			}
			.touch-input,
			.touch-button {
				width: 100%;
				max-width: none;
			}
		}
	</style>
</head>
<body>
	<div class="wrap">
		<h1>Revend Event Monitor</h1>
		<div id="heroSection" class="hero hidden">
			<div>
				<div>Monitorowanie zdarzeń + synchronizacja co minutę</div>
			</div>
		</div>
		<div class="grid">
			<section id="setupSection" class="card setup setup-screen hidden">
				<div class="setup-logo-wrap">
					<img class="setup-logo" src="/assets/logo-revend.png" alt="ReVend logo" />
				</div>
				<h2>Ustaw numer maszyny</h2>
				<p>Wpisz numer seryjny, żeby dokończyć konfigurację.</p>
				<div class="row">
					<input class="touch-input" id="machine" placeholder="np. RVM_500_123" />
					<button class="touch-button" id="saveMachine">Zapisz</button>
				</div>
				<div id="setupFeedback" class="setup-feedback"></div>
			</section>

			<section id="configSection" class="card config hidden">
				<div class="card-head">
					<h2>Tabela konfiguracji</h2>
					<button id="reloadConfigButton" class="btn-secondary" type="button">Restart konfiguracji</button>
				</div>
				<div class="table-wrap">
					<table>
						<thead><tr><th>Klucz</th><th>Wartość</th><th>Zaktualizowano</th></tr></thead>
						<tbody id="configBody"></tbody>
					</table>
				</div>
			</section>

			<section id="eventsSection" class="card events hidden">
				<h2>Zdarzenia przychodzące/wychodzące</h2>
				<div class="table-wrap">
					<table>
						<thead>
							<tr>
								<th>ID</th>
								<th>Typ</th>
								<th>Status</th>
								<th>Payload wysłany do API</th>
								<th>URL przekazania</th>
								<th>Odpowiedź</th>
								<th>Zaktualizowano</th>
							</tr>
						</thead>
						<tbody id="eventsBody"></tbody>
					</table>
				</div>
			</section>

		</div>
	</div>
	<div class="window-author">Created by Szymon Szymczyna</div>
	<div id="easterOverlay" class="easter-overlay" aria-hidden="true">
		<canvas id="easterCanvas"></canvas>
		<div id="easterMessage" class="easter-message">Konfiguracja zakończona</div>
	</div>
	<div id="appModal" class="modal-overlay" aria-hidden="true">
		<div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
			<h3 id="modalTitle" class="modal-title"></h3>
			<p id="modalText" class="modal-text"></p>
			<div class="modal-actions">
				<button id="modalCancel" class="modal-btn cancel" type="button">Anuluj</button>
				<button id="modalConfirm" class="modal-btn confirm" type="button">Potwierdź</button>
			</div>
		</div>
	</div>

	<script>
		const UI_VERSION = '__UI_VERSION__';
		(async function cacheGuard() {
			try {
				const key = 'revend_ui_version';
				const previous = localStorage.getItem(key);
				if (previous !== UI_VERSION) {
					if ('caches' in window) {
						const names = await caches.keys();
						await Promise.all(names.map(name => caches.delete(name)));
					}
					localStorage.setItem(key, UI_VERSION);
				}
			} catch {
				// Ignore cache clear errors.
			}
		})();

		const machineInput = document.getElementById("machine");
		const heroSection = document.getElementById("heroSection");
		const setupSection = document.getElementById("setupSection");
		const configSection = document.getElementById("configSection");
		const eventsSection = document.getElementById("eventsSection");
		const saveMachineButton = document.getElementById('saveMachine');
		const reloadConfigButton = document.getElementById('reloadConfigButton');
		const setupFeedback = document.getElementById('setupFeedback');
		const configBody = document.getElementById("configBody");
		const eventsBody = document.getElementById("eventsBody");
		const easterOverlay = document.getElementById('easterOverlay');
		const easterCanvas = document.getElementById('easterCanvas');
		const easterMessage = document.getElementById('easterMessage');
		const appModal = document.getElementById('appModal');
		const modalTitle = document.getElementById('modalTitle');
		const modalText = document.getElementById('modalText');
		const modalCancel = document.getElementById('modalCancel');
		const modalConfirm = document.getElementById('modalConfirm');

		async function loadMachine() {
			const res = await fetch('/api/setup/machine');
			const json = await res.json();
			if (json.isConfigured) {
				document.body.classList.remove('setup-mode');
				setupSection.classList.add('hidden');
				heroSection.classList.remove('hidden');
				configSection.classList.remove('hidden');
				eventsSection.classList.remove('hidden');
			} else {
				document.body.classList.add('setup-mode');
				machineInput.value = '';
				setupSection.classList.remove('hidden');
				heroSection.classList.add('hidden');
				configSection.classList.add('hidden');
				eventsSection.classList.add('hidden');
			}
		}

		async function saveMachine() {
			const machineNumber = machineInput.value.trim();
			if (!machineNumber) {
				setSetupFeedback('Numer seryjny jest wymagany', 'err');
				return;
			}

			saveMachineButton.disabled = true;
			saveMachineButton.textContent = 'Zapisywanie...';
			setSetupFeedback('Zapisywanie numeru seryjnego i pobieranie konfiguracji...', 'warn');

			try {
				const res = await fetch('/api/setup/machine', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ machineNumber })
				});

				const json = await safeJson(res);
				if (!res.ok) {
					setSetupFeedback((json && json.error) ? json.error : 'Błąd zapisu numeru seryjnego', 'err');
					return;
				}

				runSuccessEasterEgg();

				await loadMachine();
				if (!setupSection.classList.contains('hidden')) {
					await loadMachine();
				}
				await loadConfig();
				await loadEvents();

				if (json && json.configSync) {
					if (json.configSync.success) {
						setSetupFeedback(`Zapisano. Wczytano konfigurację (${json.configSync.updatedCount} kluczy).`, 'ok');
					} else if (json.configSync.skipped) {
						setSetupFeedback(`Zapisano. Pominięto konfigurację: ${json.configSync.message}`, 'warn');
					} else {
						setSetupFeedback(`Zapisano, ale wczytanie konfiguracji nie powiodło się: ${json.configSync.message}`, 'err');
					}
				} else {
					setSetupFeedback('Numer seryjny zapisany.', 'ok');
				}
			} catch (error) {
				setSetupFeedback(`Błąd: ${error.message || error}`, 'err');
			} finally {
				saveMachineButton.disabled = false;
				saveMachineButton.textContent = 'Zapisz';
			}
		}

		function setSetupFeedback(message, type) {
			setupFeedback.textContent = message || '';
			setupFeedback.className = `setup-feedback ${type ? `msg-${type}` : ''}`;
		}

		async function safeJson(response) {
			const text = await response.text();
			if (!text) return null;
			try { return JSON.parse(text); } catch { return null; }
		}

		async function loadConfig() {
			const res = await fetch('/api/monitor/config');
			const json = await res.json();
			configBody.innerHTML = '';
			for (const row of json) {
				const tr = document.createElement('tr');
				tr.innerHTML = `<td>${escapeHtml(row.key)}</td><td><code>${escapeHtml(row.value)}</code></td><td>${escapeHtml(row.updatedUtc)}</td>`;
				configBody.appendChild(tr);
			}
		}

		async function loadEvents() {
			const res = await fetch('/api/monitor/events?take=100');
			const json = await res.json();
			eventsBody.innerHTML = '';

			for (const row of json) {
				const tr = document.createElement('tr');
				const responseText = row.responseCode ? `${row.responseCode} ${row.responseBody || ''}` : (row.responseBody || '');
				const statusClass = row.status === 'forwarded'
					? 'badge badge-ok'
					: (row.status === 'received' || row.status === 'skipped' ? 'badge badge-warn' : 'badge badge-err');
				tr.innerHTML = `
					<td>${row.id}</td>
					<td>${escapeHtml(row.eventType)}</td>
					<td><span class="${statusClass}">${escapeHtml(row.status)}</span></td>
					<td><pre class="json-block">${escapeHtml(prettyJson(row.outgoingPayload || row.incomingPayload))}</pre></td>
					<td>${escapeHtml(row.forwardUrl || '')}</td>
					<td><pre class="json-block">${escapeHtml(prettyJson(responseText))}</pre></td>
					<td>${escapeHtml(row.updatedUtc)}</td>
				`;
				eventsBody.appendChild(tr);
			}
		}

		async function reloadConfigWithConfirm() {
			const confirmed = await showModalConfirm(
				'Restart konfiguracji',
				'Czy na pewno chcesz zresetować konfigurację? Ta operacja wyczyści numer seryjny i dane RuntimeConfig oraz przeniesie do ekranu konfiguracji numeru seryjnego.',
				'Zresetuj',
				'Anuluj'
			);
			if (!confirmed) {
				return;
			}

			if (reloadConfigButton) {
				reloadConfigButton.disabled = true;
				reloadConfigButton.textContent = 'Trwa restart...';
			}

			try {
				const res = await fetch('/api/monitor/config/reload', { method: 'POST' });
				const data = await safeJson(res);
				if (!res.ok) {
					await showModalInfo('Błąd', (data && data.error) ? data.error : 'Nie udało się zresetować konfiguracji.');
					return;
				}

				await loadMachine();
				await loadConfig();
				await loadEvents();
				runResetEasterEgg();
			} catch (error) {
				await showModalInfo('Błąd', `Błąd resetu konfiguracji: ${error.message || error}`);
			} finally {
				if (reloadConfigButton) {
					reloadConfigButton.disabled = false;
					reloadConfigButton.textContent = 'Restart konfiguracji';
				}
			}
		}

		function showModalConfirm(title, message, confirmLabel, cancelLabel) {
			return new Promise(resolve => {
				if (!appModal || !modalTitle || !modalText || !modalCancel || !modalConfirm) {
					resolve(false);
					return;
				}

				modalTitle.textContent = title;
				modalText.textContent = message;
				modalConfirm.textContent = confirmLabel;
				modalCancel.textContent = cancelLabel;
				modalCancel.style.display = '';
				appModal.classList.add('show');

				const cleanup = () => {
					modalConfirm.removeEventListener('click', onConfirm);
					modalCancel.removeEventListener('click', onCancel);
					appModal.classList.remove('show');
				};

				const onConfirm = () => {
					cleanup();
					resolve(true);
				};

				const onCancel = () => {
					cleanup();
					resolve(false);
				};

				modalConfirm.addEventListener('click', onConfirm);
				modalCancel.addEventListener('click', onCancel);
			});
		}

		function showModalInfo(title, message) {
			return new Promise(resolve => {
				if (!appModal || !modalTitle || !modalText || !modalCancel || !modalConfirm) {
					resolve();
					return;
				}

				modalTitle.textContent = title;
				modalText.textContent = message;
				modalConfirm.textContent = 'OK';
				modalCancel.style.display = 'none';
				appModal.classList.add('show');

				const onOk = () => {
					modalConfirm.removeEventListener('click', onOk);
					appModal.classList.remove('show');
					resolve();
				};

				modalConfirm.addEventListener('click', onOk);
			});
		}

		function prettyJson(value) {
			if (value == null || value === '') return '';
			const raw = String(value);
			try {
				const parsed = JSON.parse(raw);
				return JSON.stringify(parsed, null, 2);
			} catch {
				return raw;
			}
		}

		function escapeHtml(value) {
			return String(value)
				.replaceAll('&', '&amp;')
				.replaceAll('<', '&lt;')
				.replaceAll('>', '&gt;')
				.replaceAll('"', '&quot;')
				.replaceAll("'", '&#39;');
		}

		function runSuccessEasterEgg() {
			runOverlayEffect({
				message: 'Konfiguracja zakończona',
				overlayClass: 'show',
				backgroundFill: 'rgba(0, 0, 0, 0.20)',
				chars: '01ABCDEFGHIJKLMNOPQRSTUVWXYZ',
				charColor: 'rgba(110, 255, 150, 0.70)',
				burstInterval: 360,
				spawnBursts: true,
				particleHueBase: 90,
				duration: 3200,
				particleMode: 'celebration'
			});
		}

		function runResetEasterEgg() {
			runOverlayEffect({
				message: 'Konfiguracja utracona',
				overlayClass: 'show sad',
				backgroundFill: 'rgba(5, 10, 18, 0.34)',
				chars: '0101...RESET...0001',
				charColor: 'rgba(180, 198, 220, 0.38)',
				burstInterval: 0,
				spawnBursts: false,
				particleHueBase: 210,
				duration: 3000,
				particleMode: 'sad'
			});
		}

		function runOverlayEffect(options) {
			if (!easterOverlay || !easterCanvas) {
				return;
			}

			const ctx = easterCanvas.getContext('2d');
			if (!ctx) {
				return;
			}

			const dpr = Math.max(1, window.devicePixelRatio || 1);
			const particles = [];
			const chars = options.chars;
			const fontSize = 16;
			let width = 0;
			let height = 0;
			let columns = 0;
			let drops = [];
			let rafId = 0;
			let lastBurst = 0;

			function resize() {
				width = window.innerWidth;
				height = window.innerHeight;
				easterCanvas.width = Math.floor(width * dpr);
				easterCanvas.height = Math.floor(height * dpr);
				ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
				columns = Math.max(8, Math.floor(width / fontSize));
				drops = new Array(columns).fill(1);
			}

			function spawnBurst() {
				if (!options.spawnBursts) {
					return;
				}
				const cx = Math.random() * width;
				const cy = (Math.random() * height * 0.45) + (height * 0.1);
				const count = 38;
				for (let i = 0; i < count; i++) {
					const angle = (Math.PI * 2 * i) / count;
					const speed = 1.4 + Math.random() * 2.8;
					particles.push({
						x: cx,
						y: cy,
						vx: Math.cos(angle) * speed,
						vy: Math.sin(angle) * speed,
						life: 34 + Math.random() * 24,
						hue: options.particleHueBase + Math.random() * 45
					});
				}
			}

			function spawnSadParticles() {
				if (options.particleMode !== 'sad') {
					return;
				}
				for (let i = 0; i < 12; i++) {
					particles.push({
						x: Math.random() * width,
						y: -10,
						vx: (Math.random() - 0.5) * 0.3,
						vy: 1 + Math.random() * 1.2,
						life: 80 + Math.random() * 40,
						hue: 205 + Math.random() * 10,
						radius: 1.4 + Math.random() * 1.6
					});
				}
			}

			function drawFrame(ts) {
				ctx.fillStyle = options.backgroundFill;
				ctx.fillRect(0, 0, width, height);

				ctx.font = `${fontSize}px "Courier New", monospace`;
				ctx.fillStyle = options.charColor;
				for (let i = 0; i < drops.length; i++) {
					const text = chars[Math.floor(Math.random() * chars.length)];
					const x = i * fontSize;
					const y = drops[i] * fontSize;
					ctx.fillText(text, x, y);
					if (y > height && Math.random() > 0.975) {
						drops[i] = 0;
					}
					drops[i]++;
				}

				if (options.spawnBursts && (!lastBurst || ts - lastBurst > options.burstInterval)) {
					spawnBurst();
					lastBurst = ts;
				}

				if (options.particleMode === 'sad' && (!lastBurst || ts - lastBurst > 220)) {
					spawnSadParticles();
					lastBurst = ts;
				}

				for (let i = particles.length - 1; i >= 0; i--) {
					const p = particles[i];
					p.x += p.vx;
					p.y += p.vy;
					p.vy += options.particleMode === 'sad' ? 0.008 : 0.03;
					p.life -= 1;
					if (p.life <= 0) {
						particles.splice(i, 1);
						continue;
					}
					ctx.beginPath();
					ctx.fillStyle = options.particleMode === 'sad'
						? `hsla(${p.hue}, 30%, 70%, ${Math.max(0, p.life / 90)})`
						: `hsla(${p.hue}, 100%, 62%, ${Math.max(0, p.life / 60)})`;
					ctx.arc(p.x, p.y, p.radius || 2.2, 0, Math.PI * 2);
					ctx.fill();
				}

				rafId = requestAnimationFrame(drawFrame);
			}

			resize();
			if (easterMessage) {
				easterMessage.textContent = options.message;
			}
			easterOverlay.className = `easter-overlay ${options.overlayClass}`.trim();
			rafId = requestAnimationFrame(drawFrame);

			setTimeout(() => {
				cancelAnimationFrame(rafId);
				ctx.clearRect(0, 0, width, height);
				easterOverlay.className = 'easter-overlay';
			}, options.duration);
		}

		saveMachineButton.addEventListener('click', saveMachine);
		if (reloadConfigButton) {
			reloadConfigButton.addEventListener('click', reloadConfigWithConfirm);
		}

		async function refresh() {
			await Promise.all([loadMachine(), loadConfig(), loadEvents()]);
		}

		refresh();
		setInterval(refresh, 5000);
	</script>
</body>
</html>
""";
}
