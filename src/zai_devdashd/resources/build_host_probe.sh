#!/bin/sh
# SPDX-License-Identifier: MIT
# Read-only metadata for an explicitly configured build host. No log traversal.
set -u

echo PS_BEGIN
/bin/ps -axo pid=,pcpu=,etime=,comm= | /usr/bin/grep -E 'xcodebuild|xcrun|devicectl|python|swift' | /usr/bin/grep -v grep | /usr/bin/head -n 40 | while read -r pid cpu elapsed image; do
  printf 'PS\t%s\t%s\t%s\t%s\n' "$pid" "$cpu" "$elapsed" "$image"
done
echo PS_END
echo RUNS_BEGIN
echo RUNS_END
echo BP_BEGIN
echo BP_MISS
echo LIVE_BEGIN
/usr/bin/top -l 1 -n 0 | /usr/bin/awk '/^CPU usage:/ { print }'
/usr/sbin/ioreg -r -d 1 -c IOAccelerator 2>/dev/null | /usr/bin/grep 'Device Utilization %' | /usr/bin/head -n 8
/usr/bin/memory_pressure -Q 2>/dev/null | /usr/bin/grep 'free percentage'
/usr/bin/vm_stat
/usr/sbin/sysctl -n vm.swapusage 2>/dev/null
echo LIVE_END
if [ "${HW_ONCE:-0}" = 1 ]; then
  echo HW_SYS
  /usr/sbin/sysctl -n hw.model machdep.cpu.brand_string hw.ncpu hw.memsize
  echo HW_PRO
  /usr/sbin/system_profiler SPHardwareDataType 2>/dev/null | /usr/bin/grep -E 'Model Name:|Chip:|Total Number of Cores:|Memory:'
  echo HW_END
fi
echo PROBE_END
