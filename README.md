# certsync-ionos

Let's-Encrypt-Wildcard-Zertifikate per Certbot und IONOS-DNS-API, automatisch
erneuert und auf beliebige Systeme verteilt (Proxmox, AdGuard Home, UniFi OS,
Home Assistant, eigene Skripte). Konfiguration nur über Umgebungsvariablen
und/oder eine Datei auf einer Samba-Share.

## Aufbau

```
certsync   Daemon: prüft alle 12 h, holt/erneuert Zertifikate, deployt bei Änderung
samba      Share "certsync" mit  certs/  scripts/  config/
```

Share-Inhalt:

| Ordner | Inhalt |
|---|---|
| `certs/<name>/` | `fullchain.pem`, `privkey.pem`, `cert.pem`, `chain.pem` (wird bei jedem Durchlauf geschrieben, nur lesen) |
| `scripts/` | eigene Deploy-Skripte (`.sh`, `.py`) für Ziele vom Typ `script` |
| `config/certsync.env` | Konfigurationsdatei, wird automatisch neu eingelesen (Änderung löst nach ca. 30 s einen Durchlauf aus). Werte darin überschreiben Umgebungsvariablen |

`privkey.pem` ist ein Geheimnis. Die Share verlangt Login, einen anonymen
Zugriff lehnt Samba ab.

## Deployment mit Portainer

1. Portainer: **Stacks > Add stack > Repository**
   - Repository URL: die URL dieses GitHub-Repos
   - Repository reference: `refs/heads/main`
   - Compose path: `docker-compose.yml`
   - Portainer baut das Image dabei direkt aus dem Repo (`build: .`).
2. Unter **Environment variables** die Pflichtwerte eintragen (weitere siehe `.env.example`):
   `ACME_EMAIL`, `IONOS_PREFIX`, `IONOS_SECRET`, `SMB_PASSWORD`.
3. Deploy. Der Container legt die Share an und wartet auf die Konfiguration.
4. Share öffnen (`\\<docker-host>\certsync`), `config/certsync.env.example` nach
   `config/certsync.env` kopieren und anpassen: Zertifikate (`CERTS`, `CERT_*`) und
   Ziele (`TARGET_*`). Nach ca. 30 Sekunden startet der erste Durchlauf.
5. Zuerst mit `ACME_STAGING=true` testen (Standard), dann in Portainer auf `false` setzen.

Ohne Portainer: `docker compose up -d --build` mit den Variablen aus `.env.example`
als Shell-Umgebung oder in einer `.env`-Datei neben der Compose-Datei.

Das Repository enthält keine Zugangsdaten. Die Secrets stehen nur in den
Portainer-Variablen und in `config/certsync.env` auf der Share.

Die Compose-Datei nutzt weder `.env` noch `env_file`. Die globalen Werte stehen
explizit unter `environment:`. Alles Dynamische (Zertifikate, Ziele) steht in
`config/certsync.env` auf der Share. Neue Domains und Systeme brauchen so kein
Redeploy in Portainer.

## Neue Domain oder neues System

Neue Domain: in `config/certsync.env` den Namen zu `CERTS` hinzufügen und zwei Variablen anlegen.

```
CERTS=srv,smart,iot
CERT_IOT_DOMAINS=*.iot.example.net,iot.example.net
CERT_IOT_TARGETS=adguard,udm
```

Neues System: ein `TARGET_<NAME>_*` Block, der von beliebig vielen Zertifikaten
genutzt werden kann. Für nicht unterstützte Systeme gibt es den Typ `script`.

Ein Wildcard `*.srv.example.net` deckt die Basisdomain `srv.example.net` nicht ab.
Wenn du beide brauchst, führe beide auf.

## Ziel-Typen

| `TYPE` | Mechanismus | Wichtige Optionen |
|---|---|---|
| `proxmox` | API `POST /nodes/<node>/certificates/custom`, startet pveproxy neu | `url`, `token`, `nodes`, `verify_tls` |
| `adguard` | API `/control/tls/configure`, übernimmt bestehende TLS-Einstellungen | `url`, `username`, `password`, `verify_tls` |
| `unifi` | SSH: schreibt `unifi-core.crt/.key`, `systemctl restart unifi-core` | `host`, `password` oder `key_file` |
| `homeassistant` | SSH: schreibt `/ssl/fullchain.pem` und `privkey.pem`, `ha core restart` | `host`, `user`, `password` oder `key_file`, `command` |
| `homeassistant_apps` | wie `homeassistant`, startet aber nur die genannten Apps neu | `host`, `user`, `password` oder `key_file`, `apps` |
| `ssh` | beliebiger Host: Dateien hochladen, Kommando ausführen | `host`, `fullchain_path`, `privkey_path`, `command` |
| `file` | schreibt PEM-Dateien in ein gemountetes Verzeichnis | `dir`, `prefix` |
| `script` | startet dein Skript aus `scripts/` | `path`, `timeout`, beliebige weitere Optionen als `OPT_<KEY>` |

Home Assistant Apps (Add-ons), die ihr Zertifikat aus `/ssl` lesen, bekommen ein
Ziel vom Typ `homeassistant_apps` mit der Liste ihrer Slugs in `APPS`. Eine neue App
ist dann nur ein zusätzlicher Slug. Die Slugs zeigt `ha addons` in der App
"Terminal & SSH". Nur installierte und laufende Apps eintragen, ein Neustart einer
gestoppten App schlägt fehl.

Alle Optionen: `examples/certsync.env.example` bzw. `config/certsync.env.example` auf der Share.
Globale Variablen für Portainer: `.env.example`.

## Verhalten

- Zertifikat wird nur beantragt, wenn es fehlt, Domains fehlen oder es in
  `RENEW_DAYS_BEFORE_EXPIRY` (30) Tagen abläuft.
- Deploy erfolgt pro Ziel nur bei geändertem Zertifikat. Ein fehlgeschlagenes Ziel
  wird im nächsten Durchlauf erneut versucht und blockiert die anderen nicht.
- SSH-Hostschlüssel werden beim ersten Kontakt gespeichert (trust on first use),
  ein geänderter Schlüssel wird abgelehnt.

Befehle im Container: `python -m app check | once | deploy [--cert NAME] [--force]`.

## Getestet und nicht getestet

Getestet (lokal, Docker): Image-Build, Certbot mit `dns-ionos`-Plugin startet,
Konfiguration und Config-Datei, Ziele `file`, `script` und `ssh` (gegen einen
Test-SSH-Server, inklusive Kommando und Fehlerfall), Samba-Share mit Login,
Compose-Datei mit Named Volumes.

Nicht getestet: echte Zertifikatsausstellung über IONOS, sowie die Ziele
`proxmox`, `adguard`, `unifi` und `homeassistant` gegen echte Systeme.
Besonders die Pfade für UniFi OS und das Verhalten von `ha core restart` hängen
von der Firmware bzw. Version ab. Teste jedes Ziel zuerst mit `ACME_STAGING=true`.
