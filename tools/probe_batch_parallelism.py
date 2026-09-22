"""How many batchUpdates one presentation takes at once, and whether anything is lost.

The measurement behind `emit.CONTENT_WORKERS` and the layout thread: a round trip to Google
costs about a second whatever it carries, so a conversion's wall clock is round trips and not
work - and the question is whether they may overlap on one deck. They may. Measured here, on
this machine, 2026-09-22:

    8 batches x 200 requests, 1 at a time:  9.37 s
    8 batches x 200 requests, 2 at a time:  6.10 s
    8 batches x 200 requests, 4 at a time:  3.61 s
    8 batches x 200 requests, 8 at a time:  3.07 s
    layout batch beside 4 slide batches:    2.19 s
    objects: 3640/3640 written objects are there

Four is where the curve flattens, and a batch written to the layouts rides beside batches
written to slides without either losing a *request*: every object both of them create is there.
That is the whole of what this probe asks, and it was read as more than it says - what two
batches can lose without dropping a request is a value two of their requests both write, and a
slide placeholder that has not got a box of its own yet inherits its layout parent's, so the two
halves are writing one box and the later commit wins (`tools/probe_layout_race.py`, which is why
`build_deck` joins the layout pass before it dispatches a single content batch).

Makes a scratch deck in the owner's Drive, measures, and deletes it. Nothing else is touched.
Run it with `.venv\\Scripts\\python.exe tools/probe_batch_parallelism.py`.
"""

import time
from concurrent.futures import ThreadPoolExecutor

from beamer2slides.google_auth import credentials, drive_service, slides_service
from beamer2slides.gslides import execute, per_thread

BATCHES = 8
PER_BATCH = 100  # requests are 2 per shape, so ~200 each, the size of a real content batch


def shape_requests(page: str, tag: str, n: int) -> list[dict]:
    reqs = []
    for i in range(n):
        oid = f"{tag}_{i:03}"
        reqs.append({"createShape": {"objectId": oid, "shapeType": "TEXT_BOX", "elementProperties": {
            "pageObjectId": page, "size": {"width": {"magnitude": 60, "unit": "PT"},
                                           "height": {"magnitude": 12, "unit": "PT"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": 10 + 30 * (i % 20),
                          "translateY": 10 + 14 * (i // 20), "unit": "PT"}}}})
        reqs.append({"insertText": {"objectId": oid, "text": f"{tag} {i}"}})
    return reqs


def main() -> None:
    creds = credentials()
    slides, drive = slides_service(creds), drive_service(creds)
    client = per_thread(lambda: slides_service(creds))  # a service object is not thread-safe
    pid = execute(slides.presentations().create(body={"title": "b2s batch parallelism probe"}))["presentationId"]
    print(f"scratch deck {pid}")
    try:
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [
            {"createSlide": {"objectId": f"page{i:02}", "insertionIndex": i + 1,
                             "slideLayoutReference": {"predefinedLayout": "BLANK"}}}
            for i in range(BATCHES)]}))  # object ids are 5-50 characters, hence page00 and not p0
        pages = [f"page{i:02}" for i in range(BATCHES)]

        def send(tag: str, page: str) -> float:
            t = time.perf_counter()
            execute(client().presentations().batchUpdate(
                presentationId=pid, body={"requests": shape_requests(page, tag, PER_BATCH)}))
            return time.perf_counter() - t

        send("warm0", pages[0])  # the first call of a run pays for the discovery document

        for r, workers in enumerate((1, 2, 4, 8), start=1):
            t0 = time.perf_counter()
            with ThreadPoolExecutor(workers) as pool:
                each = list(pool.map(lambda i: send(f"r{r}b{i}", pages[i]), range(BATCHES)))
            print(f"{BATCHES} batches x {2 * PER_BATCH} requests, {workers} at a time: "
                  f"{time.perf_counter() - t0:5.2f} s wall, each {min(each):.2f}-{max(each):.2f} s")

        # A layout batch beside slide batches: the two halves `build_deck` overlaps.
        lay = execute(slides.presentations().get(
            presentationId=pid, fields="layouts(objectId)"))["layouts"][0]["objectId"]

        def layout_batch() -> float:
            t = time.perf_counter()
            execute(client().presentations().batchUpdate(
                presentationId=pid, body={"requests": shape_requests(lay, "lay", 40)}))
            return time.perf_counter() - t

        t0 = time.perf_counter()
        with ThreadPoolExecutor(5) as pool:
            jobs = [pool.submit(layout_batch)] + [pool.submit(send, f"mix{i}", pages[i]) for i in range(4)]
            times = [j.result() for j in jobs]
        print(f"layout batch beside 4 slide batches: {time.perf_counter() - t0:.2f} s wall "
              f"(layout {times[0]:.2f} s, slides {', '.join(f'{v:.2f}' for v in times[1:])})")

        # Everything there? A silently dropped batch would be the whole worry.
        pres = execute(slides.presentations().get(
            presentationId=pid, fields="slides(pageElements(objectId)),layouts(pageElements(objectId))"))
        made = {e["objectId"] for s in pres["slides"] for e in s.get("pageElements", [])}
        made |= {e["objectId"] for l in pres["layouts"] for e in l.get("pageElements", [])}
        want = {f"r{r}b{i}_{k:03}" for r in range(1, 5) for i in range(BATCHES) for k in range(PER_BATCH)}
        want |= {f"lay_{i:03}" for i in range(40)}
        want |= {f"mix{i}_{k:03}" for i in range(4) for k in range(PER_BATCH)}
        print(f"objects: {len(want & made)}/{len(want)} written objects are there")
    finally:
        execute(drive.files().delete(fileId=pid))
        print("scratch deck deleted")


if __name__ == "__main__":
    main()
