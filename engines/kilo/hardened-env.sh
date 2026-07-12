#!/usr/bin/env bash
# hardened-env.sh - SentiVue Oracle Kilo hardening profile (macOS/Linux twin).
# Sourced by engines/kilo/launch.sh (and therefore by conductor missions and
# every agent/terminal tab). CONFIG-LEVEL defang: switches off every call-home
# path the Kilo binary honors via environment. Defense-in-depth ONLY - the
# guarantee is the OS egress default-deny (bootstrap/harden-egress.sh). If Kilo
# ignores a knob or an upgrade adds a new endpoint, pf still makes it
# physically unreachable.
#
# Endpoints these knobs neutralize (confirmed by scanning the pinned binary,
# see engines/kilo/call-home-hosts.txt):
#   api.kilo.ai + /api/gateway   Kilo Gateway + login + org
#   ingest.kilosessions.ai, *.kiloapps.io   session ingest / cloud sharing / relay
#   us.i.posthog.com, sentry.io  product analytics / error reporting
#   models.dev                   remote model discovery + endpoint fallback
#   github releases, chocolatey  update checks
#   OTLP exporters               OpenTelemetry

# --- telemetry / analytics / tracing -----------------------------------------
export KILO_TELEMETRY_LEVEL="off"
export DISABLE_TELEMETRY="1"
export DO_NOT_TRACK="1"
export OTEL_SDK_DISABLED="true"
export OTEL_TRACES_EXPORTER="none"
export OTEL_METRICS_EXPORTER="none"
export OTEL_LOGS_EXPORTER="none"
export OTEL_EXPORTER_OTLP_ENDPOINT=""

# --- cloud sharing / session ingest / remote relay ----------------------------
export KILO_DISABLE_SHARE="1"
export KILO_AUTO_SHARE="0"
export KILO_DISABLE_SESSION_INGEST="1"
export KILO_SESSION_EXPORT_ALLOW_CUSTOM_INGEST="0"
export KILO_REMOTE="0"
export KILO_EXPERIMENTAL_WEBSOCKETS="0"
export KILO_DISABLE_EMBEDDED_WEB_UI="1"

# --- gateway / login ---------------------------------------------------------
export KILO_API_KEY=""
export KILO_ORG_ID=""

# --- update checks -----------------------------------------------------------
export KILO_DISABLE_AUTOUPDATE="1"
export KILO_ALWAYS_NOTIFY_UPDATE="0"

# --- remote model discovery / endpoint fallback ------------------------------
export KILO_DISABLE_MODELS_FETCH="1"
export KILO_ENABLE_EXPERIMENTAL_MODELS="0"

# --- external autocomplete / websearch / downloaded plugins & skills ----------
export KILO_DISABLE_LSP_DOWNLOAD="1"
export KILO_DISABLE_DEFAULT_PLUGINS="1"
export KILO_DISABLE_EXTERNAL_SKILLS="1"
export KILO_ENABLE_EXA="0"
export KILO_WEBSEARCH_PROVIDER="none"
