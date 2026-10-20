#!/usr/bin/env bash
# DELFIN's installs on a cluster, one directory per commit
# (delfin/remote/install.py). Fed over the tunnel as
#
#   bash -s -- <action> <args...>
#
#   state  DIR          one word: complete | running | failed | partial | absent
#   start  DIR PREFIX   runs install_delfin.sh in DIR, detached; prints its pid
#   list   ROOT         one line per install of DELFIN's: <commit> <state>
#   inuse               the commits the user's queued and running jobs name
#   remove DIR          deletes one install of DELFIN's (never a running one)
#   log    DIR          the last 60 lines of an install's log
#
# A directory is DELFIN's only when it holds .delfin-commit, which
# install.py writes with the uploaded tree: nothing else is ever listed or
# removed. Paths may start with "~/" (the cluster's home) because the
# arguments arrive as words, unexpanded. Prints nothing but its answer.
set -u

home_path() {
  case "$1" in
    "~") printf '%s' "$HOME" ;;
    "~/"*) printf '%s/%s' "$HOME" "${1#"~/"}" ;;
    *) printf '%s' "$1" ;;
  esac
}

state_of() {
  local dir="$1" pid
  if [ -f "$dir/.delfin-install-complete" ]; then echo complete; return; fi
  if [ -f "$dir/.delfin-install.pid" ]; then
    pid="$(cat "$dir/.delfin-install.pid" 2>/dev/null)"
    if [ -n "$pid" ] && [ ! -f "$dir/.delfin-install.exit" ] && kill -0 "$pid" 2>/dev/null; then
      echo running; return
    fi
  fi
  if [ -f "$dir/.delfin-install.exit" ]; then echo failed; return; fi
  if [ -e "$dir" ]; then echo partial; return; fi
  echo absent
}

action="${1:-}"
case "$action" in
  state)
    state_of "$(home_path "${2:?state needs a directory}")" ;;
  start)
    dir="$(home_path "${2:?start needs a directory}")"
    prefix="$(home_path "${3:?start needs a prefix}")"
    [ -f "$dir/.delfin-commit" ] && [ -f "$dir/pyproject.toml" ] \
      || { echo "no uploaded DELFIN tree in $dir" >&2; exit 2; }
    [ "$(state_of "$dir")" = running ] && { cat "$dir/.delfin-install.pid"; exit 0; }
    rm -f "$dir/.delfin-install.exit" "$dir/.delfin-install-complete"
    mkdir -p "$prefix"
    # setsid + nohup: the install outlives this ssh session and the tunnel.
    # The marker is written only when install_delfin.sh succeeds.
    nohup setsid bash -c '
      cd "$1" || exit 1
      bash delfin/installers/install_delfin.sh --repo "$1" --prefix "$2" \
        --profile standard --yes
      rc=$?
      if [ "$rc" -eq 0 ]; then date -u +%Y-%m-%dT%H:%M:%SZ > "$1/.delfin-install-complete"; fi
      echo "$rc" > "$1/.delfin-install.exit"
    ' delfin-install "$dir" "$prefix" > "$dir/.delfin-install.log" 2>&1 < /dev/null &
    echo $! > "$dir/.delfin-install.pid"
    echo $! ;;
  list)
    root="$(home_path "${2:?list needs a directory}")"
    [ -d "$root" ] || exit 0
    for d in "$root"/*/; do
      d="${d%/}"
      [ -f "$d/.delfin-commit" ] || continue
      printf '%s %s\n' "$(basename "$d")" "$(state_of "$d")"
    done ;;
  inuse)
    # install.py submits with --comment=delfin-commit=<sha>; squeue lists
    # pending, running, suspended and completing jobs.
    squeue --me -h -o '%k' 2>/dev/null \
      | grep -o 'delfin-commit=[0-9a-f]\{7,40\}' | cut -d= -f2 | sort -u ;;
  remove)
    dir="$(home_path "${2:?remove needs a directory}")"
    [ -f "$dir/.delfin-commit" ] || { echo "$dir is not an install of DELFIN's" >&2; exit 2; }
    [ "$(state_of "$dir")" = running ] && { echo "$dir is still installing" >&2; exit 3; }
    rm -rf -- "$dir" ;;
  log)
    dir="$(home_path "${2:?log needs a directory}")"
    tail -n 60 "$dir/.delfin-install.log" 2>/dev/null ;;
  *)
    echo "unknown action: $action" >&2; exit 2 ;;
esac
