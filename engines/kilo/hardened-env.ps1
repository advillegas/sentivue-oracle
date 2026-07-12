# hardened-env.ps1 - SentiVue Oracle Kilo hardening profile (Windows).
# Sourced by engines\kilo\launch.ps1 (and therefore by conductor missions and
# every agent/terminal tab). This is the CONFIG-LEVEL defang: it switches off
# every call-home path the Kilo binary honors via environment. It is
# defense-in-depth ONLY - the guarantee is the OS egress default-deny
# (bootstrap\harden-egress.ps1). If Kilo ignores a knob or an upgrade adds a new
# endpoint, the firewall still makes it physically unreachable.
#
# Endpoints these knobs neutralize (confirmed by scanning the pinned binary,
# see engines\kilo\call-home-hosts.txt):
#   api.kilo.ai / api.kilo.ai/api/gateway   Kilo Gateway + login + org
#   ingest.kilosessions.ai, *.kiloapps.io   session ingest / cloud sharing / relay
#   us.i.posthog.com, sentry.io             product analytics / error reporting
#   models.dev                              remote model discovery + endpoint fallback
#   github.com/.../releases, chocolatey     update checks
#   OTLP exporters                          OpenTelemetry

# --- telemetry / analytics / tracing -----------------------------------------
$env:KILO_TELEMETRY_LEVEL       = "off"
$env:DISABLE_TELEMETRY          = "1"
$env:DO_NOT_TRACK               = "1"     # generic opt-out many libs honor
$env:OTEL_SDK_DISABLED          = "true"  # OpenTelemetry master kill switch
$env:OTEL_TRACES_EXPORTER       = "none"
$env:OTEL_METRICS_EXPORTER      = "none"
$env:OTEL_LOGS_EXPORTER         = "none"
$env:OTEL_EXPORTER_OTLP_ENDPOINT = ""

# --- cloud sharing / session ingest / remote relay ----------------------------
$env:KILO_DISABLE_SHARE                     = "1"
$env:KILO_AUTO_SHARE                        = "0"
$env:KILO_DISABLE_SESSION_INGEST            = "1"
$env:KILO_SESSION_EXPORT_ALLOW_CUSTOM_INGEST = "0"
$env:KILO_REMOTE                            = "0"   # no real-time session relay
$env:KILO_EXPERIMENTAL_WEBSOCKETS           = "0"
$env:KILO_DISABLE_EMBEDDED_WEB_UI           = "1"   # no remote console surface

# --- gateway / login --------------------------------------------------------
# We never authenticate to Kilo's cloud: the only provider is local llama-swap
# (written into kilo.jsonc by sync-models). Blank the credential/org inputs so a
# stray env or persisted auth file cannot silently enable the gateway.
$env:KILO_API_KEY = ""
$env:KILO_ORG_ID  = ""

# --- update checks -----------------------------------------------------------
$env:KILO_DISABLE_AUTOUPDATE   = "1"
$env:KILO_ALWAYS_NOTIFY_UPDATE = "0"

# --- remote model discovery / endpoint fallback ------------------------------
# Do not fetch the models.dev catalog; do not try alternative endpoints. Only
# the models sync-models resolved on this machine are used.
$env:KILO_DISABLE_MODELS_FETCH        = "1"
$env:KILO_ENABLE_EXPERIMENTAL_MODELS  = "0"

# --- external autocomplete / websearch / downloaded plugins & skills ----------
$env:KILO_DISABLE_LSP_DOWNLOAD    = "1"   # no LSP servers pulled from GitHub at runtime
$env:KILO_DISABLE_DEFAULT_PLUGINS = "1"
$env:KILO_DISABLE_EXTERNAL_SKILLS = "1"
$env:KILO_ENABLE_EXA              = "0"   # no Exa websearch
$env:KILO_WEBSEARCH_PROVIDER      = "none"
