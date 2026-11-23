"""Wait within the observation budget for a temporarily busy accessibility owner."""


def snapshot(cli, argv, timeout_ms, *, sleep, clock):
    deadline = clock() + timeout_ms / 1000
    delay = 0.25
    args = list(argv)
    for _ in range(16):
        result = cli(*args)
        error = result.get("error") or {}
        busy_owner = error.get("code") == "TIMEOUT" and (error.get("details") or {}).get("kind") == "frames_unanswered"
        unfinished_identity = result.get("ok") and result.get("data", {}).get("identity_unproven")
        if not busy_owner and not unfinished_identity:
            return result
        remaining = deadline - clock()
        if remaining <= delay:
            return result
        sleep(delay)
        remaining = deadline - clock()
        if remaining <= 0:
            return result
        args = list(argv)
        index = args.index("--timeout-ms") + 1
        args[index] = str(max(1, int(remaining * 1000)))
        delay = min(delay * 2, 2)
    return result
