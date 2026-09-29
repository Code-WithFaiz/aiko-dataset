"""Test all GEMINI_KEY_* env vars and report which are working."""
import os
import time
from google import genai


def test_key(key: str) -> str:
    try:
        client = genai.Client(api_key=key)
        client.interactions.create(
            model="gemini-3.5-flash-lite",
            input="say hi",
        )
        return "WORKING"
    except Exception as exc:
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if status in (401, 403):
            return f"DEAD ({status})"
        if status == 429:
            return "RATE_LIMITED (429)"
        return f"ERROR ({status}): {str(exc)[:80]}"


def main() -> None:
    print("=" * 60)
    print("GEMINI KEY HEALTH CHECK")
    print("=" * 60)

    results = {}
    for i in range(1, 31):
        key = os.getenv(f"GEMINI_KEY_{i}")
        if not key:
            continue
        status = test_key(key)
        results[i] = status
        mask = f"...{key[-4:]}" if len(key) > 4 else "***"
        print(f"KEY_{i:02d} {mask} -> {status}")
        time.sleep(1)

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    working = [i for i, s in results.items() if s == "WORKING"]
    dead = [i for i, s in results.items() if "DEAD" in s]
    limited = [i for i, s in results.items() if "RATE_LIMITED" in s]
    errors = [i for i, s in results.items() if "ERROR" in s]

    print(f"Total tested:  {len(results)}")
    print(f"WORKING:       {len(working)}  -> {working}")
    print(f"DEAD:          {len(dead)}  -> {dead}")
    print(f"RATE_LIMITED:  {len(limited)}  -> {limited}")
    print(f"ERRORS:        {len(errors)}  -> {errors}")


if __name__ == "__main__":
    main()
