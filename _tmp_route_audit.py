import re
from pathlib import Path

repos = [
    Path(r"C:\Users\DELL\OneDrive\Desktop\Fanisko\Recruit_assist_website\Github\RecruitAssist-backend"),
    Path(r"C:\Users\DELL\OneDrive\Desktop\Kovetted\RecruitAssist-Backend"),
]

route_re = re.compile(
    r"@router\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']",
    re.I,
)


def collect(root: Path):
    routes = []
    app = root / "app"
    if not app.exists():
        return routes
    for p in app.rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in route_re.finditer(text):
            method, path = m.group(1).upper(), m.group(2)
            line = text[: m.start()].count("\n") + 1
            rel = str(p.relative_to(root)).replace("\\", "/")
            routes.append((method, path, rel, line))
    return routes


def in_scope(path: str) -> bool:
    return (
        path.startswith("/api/recruit")
        or path.startswith("/api/jobview")
        or path.startswith("/api/jobs")
        or path.startswith("/api/candidates")
        or path.startswith("/api/interviews")
        or path.startswith("/api/notifications/hr")
        or path.startswith("/auth/hr")
        or path.startswith("/api/hr/")
        # settings-adjacent HR surfaces often used with recruit UI
        or path == "/api/jobviewmain"
        or path.startswith("/api/upd/jobs")
        or path.startswith("/api/generate-job-")
        or path.startswith("/api/candidate_scores")
        or path.startswith("/api/manual_candidates")
        or path.startswith("/api/candidate/")  # HR views of candidates
        or path.startswith("/api/candidate_by_id")
    )


fan = collect(repos[0])
kov = collect(repos[1]) if repos[1].exists() else []
fan_rel = [(m, p, f, l) for m, p, f, l in fan if in_scope(p)]
kov_rel = [(m, p, f, l) for m, p, f, l in kov if in_scope(p)]

ra = (repos[0] / "app" / "rbac" / "route_actions.py").read_text(encoding="utf-8")
pat = re.compile(
    r'\("(GET|POST|PUT|PATCH|DELETE)",\s*r"([^"]+)",\s*C\.([A-Z_]+)\)'
)
mapped = [(m.group(1), m.group(2), m.group(3)) for m in pat.finditer(ra)]

# Resolve C constants to action strings
constants = {}
c_text = (repos[0] / "app" / "rbac" / "constants.py").read_text(encoding="utf-8")
for m in re.finditer(r'^(ACTION_[A-Z_]+)\s*=\s*"([^"]+)"', c_text, re.M):
    constants[m.group(1)] = m.group(2)


def resolve_action(method: str, path: str):
    for m, pattern, action_const in mapped:
        if m != method:
            continue
        if re.search(pattern, path):
            return constants.get(action_const, action_const), pattern
    return None, None


print("=== MAPPED PATTERNS ===")
for m, pattern, ac in mapped:
    print(f"  {m:6} {pattern:45} -> {constants.get(ac, ac)}")

print("\n=== FANISKO IN-SCOPE ROUTES ===")
unmapped = []
for method, path, f, line in sorted(fan_rel, key=lambda x: (x[1], x[0])):
    action, pattern = resolve_action(method, path)
    status = action if action else "UNMAPPED"
    print(f"{method:6} {path:55} {status:20} {f}:{line}")
    if not action:
        unmapped.append((method, path, f, line))

print(f"\n=== UNMAPPED ({len(unmapped)}) ===")
for row in unmapped:
    print(f"  {row[0]:6} {row[1]:55} {row[2]}:{row[3]}")

fan_set = {(m, p) for m, p, _, _ in fan_rel}
kov_set = {(m, p) for m, p, _, _ in kov_rel}
print("\n=== ONLY FANISKO ===")
for x in sorted(fan_set - kov_set):
    print(" ", x)
print("=== ONLY KOVETTED ===")
for x in sorted(kov_set - fan_set):
    print(" ", x)
print(f"counts fan={len(fan_rel)} kov={len(kov_rel)}")

# False positive check: does GET /api/jobs match DELETE /api/jobs/? 
print("\n=== EDGE MATCH CHECKS ===")
for method, path in [
    ("GET", "/api/jobs"),
    ("GET", "/api/jobs/abc/application-eligibility"),
    ("DELETE", "/api/jobs/abc"),
    ("GET", "/api/candidates/job123"),
    ("GET", "/api/candidates/xyz/mobile"),
    ("POST", "/api/candidates/xyz/mobile/reveal"),
    ("GET", "/api/jobview"),
    ("GET", "/api/jobview/abc/onboarding"),
    ("PATCH", "/api/jobview/abc"),
    ("POST", "/api/recruit/jobs/draft"),
    ("GET", "/api/recruit/application-policy"),
    ("PATCH", "/api/recruit/application-policy"),
    ("GET", "/api/recruit/account"),
    ("GET", "/api/interviews/job/x"),
    ("POST", "/api/interviews/invite"),
    ("GET", "/api/notifications/hr"),
]:
    a, p = resolve_action(method, path)
    print(f"  {method:6} {path:50} -> {a} via {p}")
