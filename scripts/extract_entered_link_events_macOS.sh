#!/usr/bin/env bash
set -euo pipefail

IN="${1:-data/matsim_output/output_events.xml.gz}"
OUT="${2:-results/run_01/output_events_link_events_car_truck_cut.csv}"
CUT="${3:-data/tmp/cut_links.csv}"
JOBS="${JOBS:-$(sysctl -n hw.logicalcpu 2>/dev/null || echo 8)}"
BLOCK="${BLOCK:-64M}"

mkdir -p "$(dirname "$OUT")"
printf 'time,type,link_id,vehicle\n' > "$OUT"
LC_ALL=C pigz -dc "$IN" \
  | LC_ALL=C parallel --pipe --jobs "$JOBS" --block "$BLOCK" \
    "awk -F'\"' -v CUT='$CUT' 'BEGIN{while((getline l<CUT)>0)k[l]=1} {t=\$4;s=(t==\"entered link\"||t==\"left link\");l=s?\$6:\$8;v=s?\$8:\$10} ((t==\"entered link\"||t==\"left link\"||t==\"vehicle enters traffic\"||t==\"vehicle leaves traffic\")&&v~/:(car|truck|lcv)$/&&(l in k)){printf \"%s,%s,%s,%s\\n\",\$2,t,l,v}'" >> "$OUT"
