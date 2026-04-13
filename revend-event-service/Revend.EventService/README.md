# Revend Event Service (C#)

Event-driven service for incoming webhooks and forwarding to external API.

## Features

- Webhooks:
  - `POST /webhooks/transaction` (requires `printer_barcode` or `printer_bracode` in JSON)
  - `POST /webhooks/error`
  - `POST /webhooks/bag`
  - `POST /webhooks/reset`
- Forwards webhook payloads to configured URLs.
- Stores incoming/outgoing data in SQLite.
- Pulls central config every minute and stores in table `config`.
- Setup API for machine number:
  - `GET /api/setup/machine`
  - `POST /api/setup/machine`
- Monitoring UI:
  - `GET /ui`

## Event vs scheduled

- Event-driven (triggered by webhook):
  - `transaction` -> forwards as PHP-style `transactions` payload to `/trans`
  - `error` -> forwards as PHP-style `status` payload to `/status`
  - `bag` -> forwards as PHP-style `sync_bins` payload to `/bins`
  - `reset` -> sends minimal payload `{ mid, timestamp }`
- Scheduled (every minute):
  - Config pull from `Service:MainConfigUrl`, then upsert to table `config`

## Configuration

Edit `appsettings.json`:

- `ConnectionStrings:Default` - SQLite file path
- `Service:ListenUrl` - local URL for UI + webhook receiver (default `http://localhost:21011`)
- `Service:MainConfigUrl` - URL for periodic config pull
- `Service:Api:BaseUrl` - main API base URL used when Forwarding URLs are not set
- `Service:Api:Token` - token sent as `x-api-key-machine`
- `Service:Forwarding:TransactionUrl`
- `Service:Forwarding:ErrorUrl`
- `Service:Forwarding:BagUrl`
- `Service:Forwarding:ResetUrl`

If a specific `Service:Forwarding:*Url` is set, it has priority. If it is empty, service uses `Service:Api:BaseUrl` + endpoint path (`/trans`, `/status`, `/bins`, `/reset`).

## Run

```bash
dotnet run
```

Default local URL is `http://localhost:21011`.

## Logs

- All logs are written to `logs/revend-YYYYMMDD.log` in compact JSON format.
- Logs are rotated daily.
- Old log files are automatically deleted (retention: 30 days).

## Build EXE (Windows)

From project folder run:

```bash
dotnet publish -c Release -r win-x64 --self-contained true /p:PublishSingleFile=true
```

Output executable:

```text
bin/Release/net10.0/win-x64/publish/Revend.EventService.exe
```

## Desktop window app (without browser)

Project: `../Revend.EventDesktop`

Build EXE:

```bash
cd ../Revend.EventDesktop
dotnet publish -c Release -r win-x64 --self-contained true /p:PublishSingleFile=true
```

Output:

```text
bin/Release/net10.0-windows/win-x64/publish/Revend.EventDesktop.exe
```

## Branding

UI includes a Revend-inspired color palette.

## Quick test

1. Set machine number:

```bash
curl -X POST http://localhost:21011/api/setup/machine \
  -H "Content-Type: application/json" \
  -d '{"machineNumber":"M-001"}'
```

2. Send transaction webhook:

```bash
curl -X POST http://localhost:21011/webhooks/transaction \
  -H "Content-Type: application/json" \
  -d '{"printer_barcode":"ABC123","amount":12.34}'
```

3. Open monitor:

```text
http://localhost:21011/ui
```
