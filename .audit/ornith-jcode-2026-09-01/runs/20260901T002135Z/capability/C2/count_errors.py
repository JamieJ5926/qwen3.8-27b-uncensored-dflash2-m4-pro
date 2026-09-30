import sys
import json


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: count_errors.py <file.jsonl>\n")
        sys.exit(2)

    path = sys.argv[1]

    counts = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                sys.exit(1)

            if not isinstance(obj, dict):
                sys.exit(1)

            kind = obj.get("kind")
            counts[kind] = counts.get(kind, 0) + 1

    ordered = {k: counts[k] for k in sorted(counts, key=lambda x: str(x))}
    sys.stdout.write(json.dumps(ordered))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()