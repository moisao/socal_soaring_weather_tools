#!/bin/sh
# Time the three decodes of rust/PLAN.md on cached subsets and check that
# they agree. Run from the repo root after `cargo build --release` in rust/:
#
#     rust/bench/bench.sh /data/cache/HRRR_20261115_02Z_f00.grib2 ...
#
# "cold" evicts the subset from the page cache first, so the run includes
# reading it from disk.
set -eu
bin=${CARGO_TARGET_DIR:-rust/target}/release/grib-profile
out=${SOUNDING_OUTPUT_DIR:-/tmp}/grib-bench
mkdir -p "$out"
fmt='wall %es  user %Us  sys %Ss  max RSS %M KB'

evict() {
    python3 -c 'import os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)' "$1"
}

for subset in "$@"; do
    name=$(basename "$subset" .grib2)
    echo "== $name"
    printf 'python, current (whole process)    '
    PYTHONPATH=. /usr/bin/time -f "$fmt" python3 -W ignore rust/bench/py_baseline.py "$subset" \
        2>"$out/py.err" >"$out/$name.py.json" || cat "$out/py.err"
    tail -1 "$out/py.err"; echo "    $(head -1 "$out/py.err")"
    printf 'python, streaming (whole process)  '
    /usr/bin/time -f "$fmt" python3 -W ignore rust/bench/py_streaming.py "$subset" \
        2>"$out/st.err" >"$out/$name.st.json" || cat "$out/st.err"
    tail -1 "$out/st.err"; echo "    $(head -1 "$out/st.err")"
    for run in 'cold, 4 threads:--threads 4' 'warm, 4 threads:--threads 4' 'warm, 1 thread:--threads 1' \
               'warm, grib crate, 1 thread:--threads 1 --decoder crate'; do
        label=${run%%:*}
        case $label in cold*) evict "$subset" ;; esac
        printf 'rust, %-29s' "$label"
        # shellcheck disable=SC2086
        /usr/bin/time -f "$fmt" "$bin" "$subset" --sites sites.tsv ${run#*:} 2>&1 >"$out/$name.rs.json" \
            | tail -1
    done
    printf 'streaming vs current: '; python3 rust/bench/compare.py "$out/$name.py.json" "$out/$name.st.json" | head -1
    printf 'rust vs current:      '; "$bin" "$subset" --sites sites.tsv 2>/dev/null >"$out/$name.rs.json"
    python3 rust/bench/compare.py "$out/$name.py.json" "$out/$name.rs.json" | head -1
done
