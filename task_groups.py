from typing import Any


def assign_task_ids(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(jobs, key=lambda job: (job.get("created_at", ""), job["id"]))
    by_id = {job["id"]: job for job in ordered}
    roots = {job["id"]: job["id"] for job in ordered}
    labels = {job["id"]: job.get("task_id") for job in ordered}

    def root(run_id):
        while roots[run_id] != run_id:
            roots[run_id] = roots[roots[run_id]]
            run_id = roots[run_id]
        return run_id

    def join(left, right):
        if (left["workspace"], left.get("base_url", "")) != (right["workspace"], right.get("base_url", "")):
            return
        a, b = root(left["id"]), root(right["id"])
        if a == b or (labels[a] and labels[b] and labels[a] != labels[b]):
            return
        roots[b] = a
        labels[a] = labels[a] or labels[b]

    threads, tasks = {}, {}
    for job in ordered:
        scope = (job["workspace"], job.get("base_url", ""))
        if job.get("task_id"):
            key = scope + (job["task_id"],)
            if key in tasks:
                join(tasks[key], job)
            else:
                tasks[key] = job
        for thread in (job.get("thread_id"), job.get("resume_id")):
            if thread:
                key = scope + (thread,)
                if key in threads:
                    join(threads[key], job)
                else:
                    threads[key] = job
        for field in ("parent_job_id", "summary_id"):
            parent = by_id.get(job.get(field))
            if parent:
                join(parent, job)
    groups = {}
    for job in ordered:
        groups.setdefault(root(job["id"]), []).append(job)
    for group_id, members in groups.items():
        task_id = labels[root(group_id)] or members[0]["id"]
        for job in members:
            job["task_id"] = task_id
    return ordered


def group_tasks(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = {}
    for job in sorted(jobs, key=lambda item: (item.get("created_at", ""), item["id"])):
        key = (job["workspace"], job.get("base_url", ""), job.get("task_id") or job["id"])
        groups.setdefault(key, []).append(job)
    result = []
    for (_, _, task_id), members in groups.items():
        first = next((job for job in members if job.get("kind") != "summary"), members[0])
        latest = members[-1]
        result.append({"id": task_id, "title": first["prompt"], "workspace": first["workspace"],
                       "base_url": first.get("base_url", ""), "created_at": members[0]["created_at"],
                       "updated_at": latest.get("finished_at") or latest["created_at"],
                       "status": latest["status"], "latest_job_id": latest["id"],
                       "run_ids": [job["id"] for job in members], "run_count": len(members)})
    return sorted(result, key=lambda task: (task["updated_at"], task["id"]), reverse=True)
