#!/usr/bin/env bash
set -euo pipefail

# GitHub Projects v2 logger using gh CLI.
#
# Requirements:
# - gh CLI v2.32+ with project commands
# - jq
# - Env vars: PROJECT_OWNER, PROJECT_NUMBER, GH_TOKEN/GITHUB_TOKEN
#
# Status options expected to exist in the Project:
#   Planned, In progress, Blocked, PR open, Done, Failed
#
# Usage examples:
#   PROJECT_OWNER=your-org PROJECT_NUMBER=1 \
#   GH_TOKEN=$GITHUB_TOKEN scripts/project-logger.sh start \
#     --title "Task: Fix X" --body "Short summary" --out .run/project_item_id \
#     --run-id RUN123 --model gpt-4o --branch my-branch --repo owner/repo
#
#   scripts/project-logger.sh link --item-id $(cat .run/project_item_id) \
#     --issue-url https://github.com/owner/repo/issues/123
#
#   scripts/project-logger.sh pr --item-id $(cat .run/project_item_id) \
#     --pr-url https://github.com/owner/repo/pull/456
#
#   scripts/project-logger.sh finish --item-id $(cat .run/project_item_id) \
#     --status Done --tokens-total 12345 --start-ts 1697500000 --end-ts now

command -v gh >/dev/null || { echo "Error: gh CLI not found" >&2; exit 1; }
command -v jq >/dev/null || { echo "Error: jq not found" >&2; exit 1; }

: "${PROJECT_OWNER:?set PROJECT_OWNER (org or user)}"
: "${PROJECT_NUMBER:?set PROJECT_NUMBER (project number)}"

# Prefer GH_TOKEN if present, else fall back to GITHUB_TOKEN
export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
[ -n "${GH_TOKEN:-}" ] || { echo "Error: GH_TOKEN or GITHUB_TOKEN not set" >&2; exit 1; }

log() { echo "[project-logger] $*" >&2; }

gh_project_id() {
  gh project view "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" --format json | jq -r '.id'
}

gh_project_fields() {
  local pid="$1"
  gh api graphql -f query='\
    query($id:ID!){\
      node(id:$id){... on ProjectV2{fields(first:100){nodes{\
        __typename id name\
        ... on ProjectV2SingleSelectField { options { id name } }\
      }}}}\
    }' -f id="$pid"
}

field_id() {
  local pid="$1" name="$2"
  gh_project_fields "$pid" | jq -r --arg NAME "$name" '.data.node.fields.nodes[] | select(.name==$NAME) | .id // empty'
}

single_select_option_id() {
  local pid="$1" field_name="$2" option_name="$3"
  gh_project_fields "$pid" | jq -r \
    --arg FN "$field_name" --arg ON "$option_name" \
    '.data.node.fields.nodes[] | select(.__typename=="ProjectV2SingleSelectField" and .name==$FN) | .options[] | select(.name==$ON) | .id // empty'
}

set_status() {
  local item_id="$1" status_name="$2"
  local pid; pid=$(gh_project_id)
  local fid; fid=$(field_id "$pid" "Status")
  local oid; oid=$(single_select_option_id "$pid" "Status" "$status_name")
  if [[ -z "$fid" || -z "$oid" ]]; then
    log "Warning: could not resolve Status field/option ('$status_name'). Skipping."
    return 0
  fi
  gh project item-edit --id "$item_id" --project-id "$pid" --field-id "$fid" --single-select-option-id "$oid" >/dev/null
}

set_date_field() {
  local item_id="$1" field_name="$2" value="$3" # value: YYYY-MM-DD
  local pid; pid=$(gh_project_id)
  local fid; fid=$(field_id "$pid" "$field_name")
  if [[ -z "$fid" ]]; then log "Warning: field '$field_name' not found"; return 0; fi
  gh project item-edit --id "$item_id" --project-id "$pid" --field-id "$fid" --date "$value" >/dev/null
}

set_number_field() {
  local item_id="$1" field_name="$2" value="$3"
  local pid; pid=$(gh_project_id)
  local fid; fid=$(field_id "$pid" "$field_name")
  if [[ -z "$fid" ]]; then log "Warning: field '$field_name' not found"; return 0; fi
  gh project item-edit --id "$item_id" --project-id "$pid" --field-id "$fid" --number "$value" >/dev/null
}

set_text_field() {
  local item_id="$1" field_name="$2" value="$3"
  local pid; pid=$(gh_project_id)
  local fid; fid=$(field_id "$pid" "$field_name")
  if [[ -z "$fid" ]]; then log "Warning: field '$field_name' not found"; return 0; fi
  gh project item-edit --id "$item_id" --project-id "$pid" --field-id "$fid" --text "$value" >/dev/null
}

now_date() { date -u +%F; }
to_epoch() { [[ "$1" == "now" ]] && date -u +%s || date -u -d "@$1" +%s 2>/dev/null || date -u -d "$1" +%s; }

cmd_start() {
  local title="" body="" out_file="" run_id="" model="" branch="" repo=""
  while [[ $# -gt 0 ]]; do case "$1" in
    --title) title="$2"; shift 2;;
    --body) body="$2"; shift 2;;
    --out) out_file="$2"; shift 2;;
    --run-id) run_id="$2"; shift 2;;
    --model) model="$2"; shift 2;;
    --branch) branch="$2"; shift 2;;
    --repo) repo="$2"; shift 2;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac; done
  [[ -n "$title" ]] || { echo "--title is required" >&2; exit 2; }

  local item_json item_id pid
  pid=$(gh_project_id)
  item_json=$(gh project item-create "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" --title "$title" --body "${body}" --format json)
  item_id=$(jq -r '.id' <<<"$item_json")
  log "Created draft item $item_id"

  set_status "$item_id" "In progress"
  set_date_field "$item_id" "Start date" "$(now_date)"
  [[ -n "$run_id" ]] && set_text_field "$item_id" "Run ID" "$run_id"
  [[ -n "$model" ]] && set_text_field "$item_id" "Model" "$model"
  [[ -n "$branch" ]] && set_text_field "$item_id" "Branch" "$branch"
  [[ -n "$repo" ]] && set_text_field "$item_id" "Repository" "$repo"

  if [[ -n "$out_file" ]]; then
    mkdir -p "$(dirname "$out_file")"
    echo "$item_id" > "$out_file"
  fi
  echo "$item_id"
}

cmd_link() {
  local item_id="" issue_url=""
  while [[ $# -gt 0 ]]; do case "$1" in
    --item-id) item_id="$2"; shift 2;;
    --issue-url) issue_url="$2"; shift 2;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac; done
  [[ -n "$item_id" && -n "$issue_url" ]] || { echo "--item-id and --issue-url required" >&2; exit 2; }

  # Set a text field so we keep the association on the same item.
  set_text_field "$item_id" "Issue URL" "$issue_url"
}

cmd_pr() {
  local item_id="" pr_url=""
  while [[ $# -gt 0 ]]; do case "$1" in
    --item-id) item_id="$2"; shift 2;;
    --pr-url) pr_url="$2"; shift 2;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac; done
  [[ -n "$item_id" && -n "$pr_url" ]] || { echo "--item-id and --pr-url required" >&2; exit 2; }
  set_text_field "$item_id" "PR URL" "$pr_url"
  set_status "$item_id" "PR open"
}

cmd_finish() {
  local item_id="" status="Done" tokens_total="" start_ts="" end_ts="now"
  local model="" run_id=""
  while [[ $# -gt 0 ]]; do case "$1" in
    --item-id) item_id="$2"; shift 2;;
    --status) status="$2"; shift 2;;
    --tokens-total) tokens_total="$2"; shift 2;;
    --start-ts) start_ts="$2"; shift 2;;
    --end-ts) end_ts="$2"; shift 2;;
    --model) model="$2"; shift 2;;
    --run-id) run_id="$2"; shift 2;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac; done
  [[ -n "$item_id" ]] || { echo "--item-id required" >&2; exit 2; }

  set_status "$item_id" "$status"
  set_date_field "$item_id" "End date" "$(now_date)"
  [[ -n "$tokens_total" ]] && set_number_field "$item_id" "Tokens total" "$tokens_total"
  [[ -n "$model" ]] && set_text_field "$item_id" "Model" "$model"
  [[ -n "$run_id" ]] && set_text_field "$item_id" "Run ID" "$run_id"

  # Duration minutes if timestamps provided
  if [[ -n "$start_ts" ]]; then
    local s e; s=$(to_epoch "$start_ts"); e=$(to_epoch "$end_ts")
    if [[ -n "$s" && -n "$e" ]]; then
      local dur=$(( (e - s) / 60 ))
      (( dur < 0 )) && dur=0
      set_number_field "$item_id" "Duration minutes" "$dur"
    fi
  fi
}

usage() {
  cat >&2 <<'EOF'
Usage: project-logger.sh <command> [args]
Commands:
  start   --title T [--body B] [--out FILE] [--run-id ID] [--model M] [--branch BR] [--repo OWNER/REPO]
  link    --item-id ID --issue-url URL
  pr      --item-id ID --pr-url URL
  finish  --item-id ID [--status Done|Failed] [--tokens-total N] [--start-ts EPOCH|RFC|now] [--end-ts EPOCH|RFC|now] [--model M] [--run-id ID]
EOF
}

main() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    start)  cmd_start "$@" ;;
    link)   cmd_link "$@" ;;
    pr)     cmd_pr "$@" ;;
    finish) cmd_finish "$@" ;;
    -h|--help|help|"") usage; exit 2;;
    *) echo "Unknown command: $cmd" >&2; usage; exit 2;;
  esac
}

main "$@"

