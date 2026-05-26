import sys
sys.path.insert(0, "src")
with open("src/cyrene/agent.py") as f:
    content = f.read()
start = content.index("_DEEP_RESEARCH_PROMPT = ")
body = content[start:]
body = body[body.index('"""')+3:]
end = body.index('"""')
old = body[:end]

with open("src/cyrene/agent/prompts.py") as f:
    pcontent = f.read()
pstart = pcontent.index("_DEEP_RESEARCH_PROMPT = ")
pbody = pcontent[pstart:]
pbody = pbody[pbody.index('"""')+3:]
pend = pbody.index('"""')
new = pbody[:pend]

print(f"Len old: {len(old)}, Len new: {len(new)}")
print(f"Same: {old == new}")
if old != new:
    for i, (a, b) in enumerate(zip(old, new)):
        if a != b:
            print(f"First diff at {i}: {repr(a)} vs {repr(b)}")
            break
