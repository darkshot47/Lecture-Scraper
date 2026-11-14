"""
Proxy layer ka live self-test (token ki zaroorat nahi).

Kya check karta hai:
  1. IN proxies discovery (saare sources)
  2. geo-verify: proxy se ip-api -> countryCode == IN
  3. studyratna hosts tak reachability
  4. sabse accha proxy chun kar uski public IP + ISP print karta hai

CI me chalta hai taaki proxy pool ka health pata rahe.
"""
from __future__ import annotations

import sys

import proxies


def main() -> int:
    print("=== 1) discovery ===", flush=True)
    found = proxies.discover(country_only=True, log=print)
    print(f"IN-labelled proxies: {len(found)}", flush=True)
    if len(found) < 20:
        extra = proxies.discover(country_only=False, log=print)
        print(f"global fallback: {len(extra)}", flush=True)
        found += extra[:1200]

    print("=== 2/3) geo-verify + health ===", flush=True)
    good = proxies.validate(found, workers=96, log=print)
    proxies.save_cache(good)

    print(f"\n=== RESULT: {len(good)} working IN proxies "
          f"({sum(1 for g in good if g.get('host_ok'))} studyratna-reachable) ===")
    for g in good[:10]:
        print(f"  {g['proxy']:35s} ip={g.get('ip')} isp={g.get('isp')} "
              f"reach={g.get('reachable')}")
    if not good:
        print("FAIL: koi IN proxy nahi mila (free pool empty ho sakta hai)")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
