import multiprocessing
import os
import time

import RNS
import RNS.vendor.umsgpack as msgpack

WORKBLOCK_EXPAND_ROUNDS = 3000

active_jobs = {}

def stamp_workblock(message_id):
    wb_st = time.time()
    expand_rounds = WORKBLOCK_EXPAND_ROUNDS
    workblock = b""
    for n in range(expand_rounds):
        workblock += RNS.Cryptography.hkdf(
            length=256,
            derive_from=message_id,
            salt=RNS.Identity.full_hash(message_id+msgpack.packb(n)),
            context=None,
        )
    wb_time = time.time() - wb_st
    RNS.log(f"Stamp workblock size {RNS.prettysize(len(workblock))}, generated in {round(wb_time*1000,2)}ms", RNS.LOG_DEBUG)

    return workblock

def stamp_value(workblock, stamp):
    value = 0
    bits = 256
    material = RNS.Identity.full_hash(workblock+stamp)
    i = int.from_bytes(material, byteorder="big")
    while ((i & (1 << (bits - 1))) == 0):
        i = (i << 1)
        value += 1

    return value

def generate_stamp(message_id, stamp_cost):
    RNS.log(f"Generating stamp with cost {stamp_cost} for {RNS.prettyhexrep(message_id)}...", RNS.LOG_DEBUG)
    workblock = stamp_workblock(message_id)

    start_time = time.time()
    stamp = None
    rounds = 0
    value = 0

    if RNS.vendor.platformutils.is_windows() or RNS.vendor.platformutils.is_darwin():
        stamp, rounds = job_simple(stamp_cost, workblock, message_id)

    elif RNS.vendor.platformutils.is_android():
        stamp, rounds = job_android(stamp_cost, workblock, message_id)

    else:
        stamp, rounds = job_linux(stamp_cost, workblock, message_id)

    duration = time.time() - start_time
    speed = rounds/duration
    if stamp is not None:
        value = stamp_value(workblock, stamp)

    RNS.log(f"Stamp with value {value} generated in {RNS.prettytime(duration)}, {rounds} rounds, {int(speed)} rounds per second", RNS.LOG_DEBUG)

    return stamp, value

def cancel_work(message_id):
    if RNS.vendor.platformutils.is_windows() or RNS.vendor.platformutils.is_darwin():
        try:
            if message_id in active_jobs:
                active_jobs[message_id] = True

        except Exception as e:
            RNS.log("Error while terminating stamp generation workers: {e}", RNS.LOG_ERROR)
            RNS.trace_exception(e)

    elif RNS.vendor.platformutils.is_android():
        try:
            if message_id in active_jobs:
                active_jobs[message_id] = True

        except Exception as e:
            RNS.log("Error while terminating stamp generation workers: {e}", RNS.LOG_ERROR)
            RNS.trace_exception(e)

    else:
        try:
            if message_id in active_jobs:
                stop_event = active_jobs[message_id][0]
                result_queue = active_jobs[message_id][1]
                stop_event.set()
                result_queue.put(None)
                active_jobs.pop(message_id)

        except Exception as e:
            RNS.log("Error while terminating stamp generation workers: {e}", RNS.LOG_ERROR)
            RNS.trace_exception(e)

def job_simple(stamp_cost, workblock, message_id):
    # A simple, single-process stamp generator.
    # should work on any platform, and is used
    # as a fall-back, in case of limited multi-
    # processing and/or acceleration support.

    platform = RNS.vendor.platformutils.get_platform()
    RNS.log(f"Running stamp generation on {platform}, work limited to single CPU core. This will be slower than ideal.", RNS.LOG_WARNING)

    rounds = 0
    pstamp = os.urandom(256//8)
    st = time.time()

    active_jobs[message_id] = False

    def sv(s, c, w):
        target = 0b1<<256-c
        m = w+s
        result = RNS.Identity.full_hash(m)
        if int.from_bytes(result, byteorder="big") > target:
            return False
        else:
            return True

    while not sv(pstamp, stamp_cost, workblock) and not active_jobs[message_id]:
        pstamp = os.urandom(256//8)
        rounds += 1
        if rounds % 2500 == 0:
            speed = rounds / (time.time()-st)
            RNS.log(f"Stamp generation running. {rounds} rounds completed so far, {int(speed)} rounds per second", RNS.LOG_DEBUG)

    if active_jobs[message_id]:
        pstamp = None

    active_jobs.pop(message_id)

    return pstamp, rounds

def job_linux(stamp_cost, workblock, message_id):
    allow_kill = True
    stamp = None
    total_rounds = 0
    jobs = multiprocessing.cpu_count()
    stop_event   = multiprocessing.Event()
    result_queue = multiprocessing.Queue(1)
    rounds_queue = multiprocessing.Queue()

    def job(stop_event, pn, sc, wb):
        rounds = 0
        pstamp = os.urandom(256//8)

        def sv(s, c, w):
            target = 0b1<<256-c
            m = w+s
            result = RNS.Identity.full_hash(m)
            if int.from_bytes(result, byteorder="big") > target:
                return False
            else:
                return True

        while not stop_event.is_set() and not sv(pstamp, sc, wb):
            pstamp = os.urandom(256//8)
            rounds += 1

        if not stop_event.is_set():
            stop_event.set()
            result_queue.put(pstamp)
        rounds_queue.put(rounds)

    job_procs = []
    RNS.log(f"Starting {jobs} stamp generation workers", RNS.LOG_DEBUG)
    for jpn in range(jobs):
        process = multiprocessing.Process(target=job, kwargs={"stop_event": stop_event, "pn": jpn, "sc": stamp_cost, "wb": workblock}, daemon=True)
        job_procs.append(process)
        process.start()

    active_jobs[message_id] = [stop_event, result_queue]

    stamp = result_queue.get()
    RNS.log("Got stamp result from worker", RNS.LOG_DEBUG) # TODO: Remove

    # Collect any potential spurious
    # results from worker queue.
    try:
        while True:
            result_queue.get_nowait()
    except Exception as e:
        RNS.log(f"Exception while draining result_queue: {e}", RNS.LOG_DEBUG)

    for j in range(jobs):
        nrounds = 0
        try:
            nrounds = rounds_queue.get(timeout=2)
        except Exception as e:
            RNS.log(f"Failed to get round stats part {j}: {e}", RNS.LOG_ERROR)
        total_rounds += nrounds

    all_exited = False
    exit_timeout = time.time() + 5
    while time.time() < exit_timeout:
        if not any(p.is_alive() for p in job_procs):
            all_exited = True
            break
        time.sleep(0.1)

    if not all_exited:
        RNS.log("Stamp generation IPC timeout, possible worker deadlock. Terminating remaining processes.", RNS.LOG_ERROR)
        if allow_kill:
            for j in range(jobs):
                process = job_procs[j]
                process.kill()
        else:
            return None

    else:
        for j in range(jobs):
            process = job_procs[j]
            process.join()
            # RNS.log(f"Joined {j} / {process}", RNS.LOG_DEBUG) # TODO: Remove

    return stamp, total_rounds

def job_android(stamp_cost, workblock, message_id):
    # Semaphore support is flaky to non-existent on
    # Android, so we need to manually dispatch and
    # manage workloads here, while periodically
    # checking in on the progress.

    stamp = None
    start_time = time.time()
    total_rounds = 0
    rounds_per_worker = 1000

    use_nacl = False
    try:
        import nacl.encoding
        import nacl.hash
        use_nacl = True
    except Exception as e:
        RNS.log(f"Exception while importing nacl: {e}", RNS.LOG_DEBUG)

    if use_nacl:
        def full_hash(m):
            return nacl.hash.sha256(m, encoder=nacl.encoding.RawEncoder)
    else:
        def full_hash(m):
            return RNS.Identity.full_hash(m)

    def sv(s, c, w):
        target = 0b1<<256-c
        m = w+s
        result = full_hash(m)
        if int.from_bytes(result, byteorder="big") > target:
            return False
        else:
            return True

    wm = multiprocessing.Manager()
    jobs = multiprocessing.cpu_count()

    def job(procnum=None, results_dict=None, wb=None, sc=None, jr=None):
        # RNS.log(f"Worker {procnum} starting for {jr} rounds...") # TODO: Remove
        try:
            rounds = 0
            found_stamp = None

            while True:
                pstamp = os.urandom(256//8)
                rounds += 1
                if sv(pstamp, sc, wb):
                    found_stamp = pstamp
                    break

                if rounds >= jr:
                    # RNS.log(f"Worker {procnum} found no result in {rounds} rounds") # TODO: Remove
                    break

            results_dict[procnum] = [found_stamp, rounds]
        except Exception as e:
            RNS.log(f"Stamp generation worker error: {e}", RNS.LOG_ERROR)
            RNS.trace_exception(e)

    active_jobs[message_id] = False

    RNS.log(f"Dispatching {jobs} workers for stamp generation...", RNS.LOG_DEBUG) # TODO: Remove

    results_dict = wm.dict()
    while stamp is None and not active_jobs[message_id]:
        job_procs = []
        try:
            for pnum in range(jobs):
                pargs = {"procnum":pnum, "results_dict": results_dict, "wb": workblock, "sc":stamp_cost, "jr":rounds_per_worker}
                process = multiprocessing.Process(target=job, kwargs=pargs)
                job_procs.append(process)
                process.start()

            for process in job_procs:
                process.join()

            for j in results_dict:
                r = results_dict[j]
                total_rounds += r[1]
                if r[0] is not None:
                    stamp = r[0]

            if stamp is None:
                elapsed = time.time() - start_time
                speed = total_rounds/elapsed
                RNS.log(f"Stamp generation running. {total_rounds} rounds completed so far, {int(speed)} rounds per second", RNS.LOG_DEBUG)

        except Exception as e:
            RNS.log(f"Stamp generation job error: {e}")
            RNS.trace_exception(e)

    active_jobs.pop(message_id)

    return stamp, total_rounds

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        RNS.log("No cost argument provided", RNS.LOG_ERROR)
        exit(1)
    else:
        try:
            cost = int(sys.argv[1])
        except Exception as e:
            RNS.log(f"Invalid cost argument provided: {e}", RNS.LOG_ERROR)
            exit(1)

    RNS.loglevel = RNS.LOG_DEBUG
    RNS.log("Testing LXMF stamp generation", RNS.LOG_DEBUG)
    message_id = os.urandom(32)
    generate_stamp(message_id, cost)
