#!/bin/sh
# env: PW_KEY, LAT, LON, NTFY_URL (server + topic, e.g. https://ntfy.sh/mytopic),
#      NTFY_TOKEN (optional; tk_... token for protected topics)
# optional DEBUG: 0 = normal, 1 = extra log detail,
#                 2 = extra detail + one test notification per container start
# Alert thresholds (all optional, defaults shown):
#   LOOKAHEAD_MIN   how far ahead to look, in minutes (max 60)
#   MIN_PROB        minimum chance of rain in a minute, 0-1 (0.5 = 50%)
#   MIN_INTENSITY   minimum rain rate in that minute, mm/h (0.5 ≈ light rain)
#   RAINING_NOW     current rate (mm/h) above which we assume it's already raining
LOOKAHEAD_MIN=${LOOKAHEAD_MIN:-30}
MIN_PROB=${MIN_PROB:-0.5}
MIN_INTENSITY=${MIN_INTENSITY:-0.5}
RAINING_NOW=${RAINING_NOW:-0.1}
#   REPEAT_MIN      re-send the alert every N minutes while rain is still predicted (0 = once only)
REPEAT_MIN=${REPEAT_MIN:-0}
#   SCOPE_HOSTS     optional comma-separated list of telescope IPs/hostnames (host or host:port,
#                   default port 4700). If set, alerts only fire while at least one is reachable,
#                   i.e. powered on and on the network.
SCOPE_HOSTS=${SCOPE_HOSTS:-}
STATE=/state/alerted
TEST_MARKER=/tmp/test_sent
DEBUG=${DEBUG:-0}

log()   { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
debug() { [ "$DEBUG" -ge 1 ] 2>/dev/null && log "DEBUG $*"; }

# notify TITLE MESSAGE PRIORITY TAGS — same code path for real alerts and tests
# Token is optional (public ntfy.sh topics don't need one); logs ntfy's reply on failure.
notify() {
  title=$1; msg=$2; prio=$3; tags=$4
  set -- -s -o /tmp/ntfy_resp -w '%{http_code}' \
         -H "Title: $title" -H "Priority: $prio" -H "Tags: $tags"
  [ -n "$NTFY_TOKEN" ] && set -- "$@" -H "Authorization: Bearer $NTFY_TOKEN"
  code=$(curl "$@" -d "$msg" "$NTFY_URL")
  [ "$code" = "200" ] && return 0
  log "ntfy returned HTTP ${code:-000} (000 = couldn't connect): $(head -c 300 /tmp/ntfy_resp 2>/dev/null)"
  return 1
}

# Scope gate: skip everything (including the API call) unless a scope is online
if [ -n "$SCOPE_HOSTS" ]; then
  online=""
  for h in $(echo "$SCOPE_HOSTS" | tr ',' ' '); do
    host=${h%%:*}; port=${h#*:}; [ "$port" = "$h" ] && port=4700
    if nc -z -w 2 "$host" "$port" 2>/dev/null; then
      online="$online $host"
    fi
  done
  if [ -z "$online" ]; then
    log "no scope online ($SCOPE_HOSTS), not checking"
    rm -f "$STATE"   # so the next session starts fresh
    exit 0
  fi
  debug "scope(s) online:$online"
fi

debug "checking forecast for $LAT,$LON (lookahead=${LOOKAHEAD_MIN}min prob>$MIN_PROB intensity>${MIN_INTENSITY}mm/h raining_now>=${RAINING_NOW}mm/h repeat=${REPEAT_MIN}min)"

json=$(curl -sf "https://api.pirateweather.net/forecast/$PW_KEY/$LAT,$LON?units=si&exclude=hourly,daily,alerts")
if [ $? -ne 0 ] || [ -z "$json" ]; then
  log "ERROR fetching Pirate Weather (bad key, quota, or network?)"
  exit 0
fi

# Summary of what the forecast says right now
now=$(echo "$json" | jq -r '.currently.precipIntensity // 0')
maxprob=$(echo "$json" | jq -r --argjson look "$LOOKAHEAD_MIN" '[(.minutely.data // [])[:$look][].precipProbability] | max // 0 | . * 100 | floor')
maxint=$(echo "$json" | jq -r --argjson look "$LOOKAHEAD_MIN" '[(.minutely.data // [])[:$look][].precipIntensity] | max // 0')
mins=$(echo "$json" | jq -r '(.minutely.data // []) | length')
debug "got $mins minutely entries"

# Minutes until likely rain within LOOKAHEAD_MIN; empty if none or already raining.
eta=$(echo "$json" | jq -r --argjson look "$LOOKAHEAD_MIN" --argjson prob "$MIN_PROB" \
        --argjson inten "$MIN_INTENSITY" --argjson now "$RAINING_NOW" '
  if (.currently.precipIntensity // 0) >= $now then empty
  else [(.minutely.data // [])[:$look] | to_entries[]
        | select(.value.precipProbability > $prob and .value.precipIntensity > $inten)
        | .key] | first // empty
  end')

summary="now=${now}mm/h next${LOOKAHEAD_MIN}: max_prob=${maxprob}% max_int=${maxint}mm/h eta=${eta:-none}"
log "$summary"

# DEBUG=2: send one test notification per container start to prove the ntfy path works
if [ "$DEBUG" -ge 2 ] 2>/dev/null && [ ! -f "$TEST_MARKER" ]; then
  if notify "Rain alert test" "Test from rain-alert. $summary" "high" "loud_sound,bell"; then
    touch "$TEST_MARKER"
    log "TEST notification sent"
  else
    log "ERROR sending TEST to ntfy at $NTFY_URL (token, cert, or reachability?)"
  fi
fi

if [ -n "$eta" ]; then
  if [ ! -f "$STATE" ]; then
    if notify "Rain incoming" "Rain expected in about $eta min" "high" "loud_sound,bell"; then
      touch "$STATE"
      log "ALERT sent: rain in ~${eta} min"
    else
      log "ERROR sending to ntfy at $NTFY_URL (token, cert, or reachability?)"
    fi
  elif [ "$REPEAT_MIN" -gt 0 ] 2>/dev/null && \
       [ $(( $(date +%s) - $(stat -c %Y "$STATE") )) -ge $(( REPEAT_MIN * 60 )) ]; then
    if notify "Rain incoming (still)" "Rain expected in about $eta min" "high" "loud_sound,bell"; then
      touch "$STATE"   # resets the repeat timer
      log "REPEAT alert sent: rain in ~${eta} min"
    else
      log "ERROR sending repeat to ntfy at $NTFY_URL"
    fi
  else
    log "rain expected in ~${eta} min, already alerted, skipping"
  fi
elif [ -f "$STATE" ]; then
  rm -f "$STATE"
  log "threat passed or raining now, alert re-armed"
fi
