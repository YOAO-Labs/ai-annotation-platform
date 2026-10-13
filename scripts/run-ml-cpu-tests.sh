#!/usr/bin/env bash
#
# CPU test runner for the reusable `.github/workflows/ml-cpu-test.yml` workflow.
#
# Isolated per suite: every invocation creates its own throwaway virtualenv, so
# no suite can inherit another package's `sys.modules` (the committed tests
# install MagicMock torch stubs, and a shared interpreter would let a stub leak
# into a later suite). No GPU, weights or network are used by the tests.
#
# Usage:
#   scripts/run-ml-cpu-tests.sh plan "<spec>"   # print name=value suite flags
#   scripts/run-ml-cpu-tests.sh run <suite>     # install deps and run pytest
#   scripts/run-ml-cpu-tests.sh list            # list suite names
#
# `<spec>` is `all` (default) or a comma/space separated subset of the suite
# names. Unknown names fail loudly.
#
# Set ML_CPU_DRY_RUN=1 to print the commands instead of executing them.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS_DIR="$ROOT/scripts/ml-cpu-deps"
TORCH_CPU_INDEX="https://download.pytorch.org/whl/cpu"

SUITES=(
  shared-backend-runtime
  shared-mask-utils
  shared-protocol-v2
  yolo
  rapidocr
  onnxtools
  grounded-sam2
  sam3
)

VENV_DIRS=()
cleanup_venvs() {
  local dir
  for dir in ${VENV_DIRS[@]+"${VENV_DIRS[@]}"}; do
    [ -n "$dir" ] && rm -rf "$dir"
  done
  return 0
}
trap cleanup_venvs EXIT

log() { printf '==> %s\n' "$*" >&2; }

run_in() {
  local dir="$1"
  shift
  if [ "${ML_CPU_DRY_RUN:-0}" = "1" ]; then
    printf '+ (cd %s && %s)\n' "$dir" "$*" >&2
  else
    (cd "$ROOT/$dir" && "$@")
  fi
}

# new_venv sets the global VENV_PATH instead of printing on stdout: it must run
# in the main shell (not a command substitution) so VENV_DIRS recorded here is
# the same array the EXIT trap cleans up.
VENV_PATH=""
new_venv() {
  local python_version="$1"
  if [ "${ML_CPU_DRY_RUN:-0}" = "1" ]; then
    VENV_PATH="<venv-python-$python_version>"
    return 0
  fi
  local base
  base="$(mktemp -d "${TMPDIR:-/tmp}/ml-cpu-test.XXXXXX")"
  VENV_DIRS+=("$base")
  uv venv --python "$python_version" "$base/venv" >&2
  VENV_PATH="$base/venv"
  return 0
}

# plan: expand and validate "<spec>" into GitHub-output style suite flags.
plan() {
  local spec="${1-all}"
  local -a requested=()
  if [ "$spec" = "all" ]; then
    requested=("${SUITES[@]}")
  else
    # Normalize every accepted separator (comma, space, tab, CR, LF) to a single
    # space, then split with `read -a`. `read -a` performs no pathname expansion,
    # so a literal `*` stays one unknown token instead of globbing the checkout.
    local normalized="$spec"
    normalized="${normalized//,/ }"
    normalized="${normalized//$'\t'/ }"
    normalized="${normalized//$'\r'/ }"
    normalized="${normalized//$'\n'/ }"
    requested=()
    read -r -a requested <<< "$normalized" || true
    # Explicit nonempty invariant: the string guard cannot see tab/newline-only
    # input, so check the parsed array instead of consulting the raw string.
    if [ "${#requested[@]}" -eq 0 ]; then
      printf 'no suites selected: pass "all" or at least one suite name (got %q)\n' "$spec" >&2
      exit 1
    fi
    local token
    for token in "${requested[@]}"; do
      local known=0
      local suite
      for suite in "${SUITES[@]}"; do
        if [ "$token" = "$suite" ]; then
          known=1
          break
        fi
      done
      if [ "$known" -ne 1 ]; then
        printf 'unknown suite %q; expected one of: %s\n' "$token" "${SUITES[*]}" >&2
        exit 1
      fi
    done
  fi
  local suite
  for suite in "${SUITES[@]}"; do
    local value="false"
    local wanted
    for wanted in "${requested[@]}"; do
      if [ "$wanted" = "$suite" ]; then
        value="true"
        break
      fi
    done
    printf '%s=%s\n' "${suite//-/_}" "$value"
  done
}

# Both installers set the global VENV_PATH and must be called as statements.
install_editable() {
  local dir="$1" python_version="$2" extras="$3"
  new_venv "$python_version"
  log "install $dir editable [$extras] (python $python_version)"
  run_in "$dir" uv pip install --python "$VENV_PATH/bin/python" -e ".[$extras]"
}

install_file() {
  local dir="$1" python_version="$2" file="$3"
  shift 3
  new_venv "$python_version"
  log "install $file (python $python_version)"
  run_in "$dir" uv pip install --python "$VENV_PATH/bin/python" -r "$DEPS_DIR/$file" "$@"
}

run_pytest() {
  local dir="$1" venv="$2"
  shift 2
  log "pytest $dir $*"
  run_in "$dir" "$venv/bin/python" -m pytest -q "$@"
}

# run: install the minimal dependencies for one suite and execute its tests.
run() {
  local suite="${1:?suite name required}"
  case "$suite" in
    shared-backend-runtime)
      install_editable "apps/_shared/backend_runtime" "3.10" "test"
      run_pytest "apps/_shared/backend_runtime" "$VENV_PATH"
      ;;
    shared-mask-utils)
      install_editable "apps/_shared/mask_utils" "3.10" "test"
      run_pytest "apps/_shared/mask_utils" "$VENV_PATH"
      ;;
    shared-protocol-v2)
      install_editable "apps/_shared/protocol_v2" "3.10" "test"
      run_pytest "apps/_shared/protocol_v2" "$VENV_PATH"
      ;;
    yolo)
      # Test-only deps: deliberately avoids the backend's ultralytics -> torch runtime stack.
      install_file "apps/yolo-backend" "3.10" "yolo-test-only.txt"
      run_pytest "apps/yolo-backend" "$VENV_PATH"
      ;;
    rapidocr)
      install_editable "apps/rapidocr-backend" "3.10" "dev"
      run_pytest "apps/rapidocr-backend" "$VENV_PATH"
      ;;
    onnxtools)
      install_editable "apps/onnxtools-backend" "3.10" "dev"
      run_in "apps/onnxtools-backend" uv pip install --python "$VENV_PATH/bin/python" -r "$DEPS_DIR/onnxtools-extra.txt"
      run_pytest "apps/onnxtools-backend" "$VENV_PATH"
      ;;
    grounded-sam2)
      install_editable "apps/grounded-sam2-backend" "3.10" "dev"
      run_in "apps/grounded-sam2-backend" uv pip install --python "$VENV_PATH/bin/python" -r "$DEPS_DIR/torch-cpu.txt" --index-url "$TORCH_CPU_INDEX"
      run_pytest "apps/grounded-sam2-backend" "$VENV_PATH"
      ;;
    sam3)
      install_editable "apps/sam3-backend" "3.12" "dev"
      run_in "apps/sam3-backend" uv pip install --python "$VENV_PATH/bin/python" -r "$DEPS_DIR/torch-cpu.txt" --index-url "$TORCH_CPU_INDEX"
      run_pytest "apps/sam3-backend" "$VENV_PATH"
      ;;
    *)
      printf 'unknown suite %q; expected one of: %s\n' "$suite" "${SUITES[*]}" >&2
      exit 1
      ;;
  esac
}

# selftest: dependency-free regression for the plan() contract. Exercises the
# separator mixtures, empty/whitespace-only rejection, unknown names, and the
# wildcard case that must not glob. Exits nonzero on any mismatch.
selftest() {
  local failures=0
  local suite_list="shared_backend_runtime,shared_mask_utils,shared_protocol_v2,yolo,rapidocr,onnxtools,grounded_sam2,sam3"

  _selftest_plan() {
    local spec="$1"
    local out
    if out="$(plan "$spec" 2>/dev/null)"; then
      printf '%s' "$out"
      return 0
    fi
    printf '%s' "$out"
    return 1
  }

  _selftest_ok() {
    local spec="$1" expected="$2"
    local out
    if ! out="$(_selftest_plan "$spec")"; then
      printf 'FAIL plan(%q): expected success\n' "$spec" >&2
      failures=$((failures + 1))
      return
    fi
    local -a trues=()
    local line
    while IFS= read -r line; do
      if [ "${line#*=}" = "true" ]; then
        trues+=("${line%%=*}")
      fi
    done <<< "$out"
    local actual=""
    if [ "${#trues[@]}" -gt 0 ]; then
      actual="$(IFS=,; printf '%s' "${trues[*]}")"
    fi
    if [ "$actual" != "$expected" ]; then
      printf 'FAIL plan(%q): expected true=[%s] got [%s]\n' "$spec" "$expected" "$actual" >&2
      failures=$((failures + 1))
    fi
  }

  _selftest_fail() {
    local spec="$1"
    local out
    if out="$(_selftest_plan "$spec")"; then
      printf 'FAIL plan(%q): expected nonzero exit, got success\n' "$spec" >&2
      failures=$((failures + 1))
      return
    fi
    if [[ "$out" == *=true* ]]; then
      printf 'FAIL plan(%q): emitted true flags on failure\n' "$spec" >&2
      failures=$((failures + 1))
    fi
  }

  _selftest_ok "all" "$suite_list"
  _selftest_ok "yolo,onnxtools" "yolo,onnxtools"
  _selftest_ok "yolo onnxtools rapidocr" "yolo,rapidocr,onnxtools"
  _selftest_ok $'yolo\tonnxtools' "yolo,onnxtools"
  _selftest_ok $'yolo\nonnxtools' "yolo,onnxtools"
  _selftest_ok $'yolo\n\t sam3,\trapidocr' "yolo,rapidocr,sam3"
  _selftest_ok "yolo,,onnxtools" "yolo,onnxtools"
  _selftest_ok "yolo,yolo" "yolo"
  _selftest_fail ""
  _selftest_fail $' \t\r\n '
  _selftest_fail ",,"
  _selftest_fail "bogus"
  _selftest_fail "yolo,bogus"
  _selftest_fail "*"

  if [ "$failures" -eq 0 ]; then
    printf 'plan selftest: all cases passed\n'
    return 0
  fi
  printf 'plan selftest: %s case(s) failed\n' "$failures" >&2
  return 1
}

command="${1:-}"
case "$command" in
  plan) plan "${2-all}" ;;
  run) run "${2:-}" ;;
  list) printf '%s\n' "${SUITES[@]}" ;;
  selftest) selftest ;;
  *)
    printf 'usage: %s {plan "<spec>"|run <suite>|list|selftest}\n' "$(basename "$0")" >&2
    exit 2
    ;;
esac
