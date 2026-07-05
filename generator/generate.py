#!/usr/bin/env python3
"""
FraudGraph Synthetic Data Foundry
=================================

Generates a synthetic transaction graph at a chosen scale (1M / 10M / 50M edges),
composed of two layers:

1. Background traffic shaped to real-world statistical patterns:
   - power-law account activity (most accounts transact rarely; a few are hubs)
   - log-normal transaction amounts, parameterized per channel
   - diurnal + weekly timestamp rhythm
   - channel mix modeled on PaySim's mobile-money schema (P2P, merchant, cash-in/out)

2. Injected fraud topologies, each with configurable counts and ground-truth labels:
   - smurfing fan-in         many small senders -> one collector, under-threshold amounts
   - dispersal fan-out       one compromised source -> many mules within hours
   - cyclic laundering ring  closed loops of 4-7 hops with value decay (fees)
   - mule chain              layering path A->B->C->D with short dwell times
   - dormant burst           aged accounts that suddenly activate inside a ring

Fraud rings are stitched into the background graph via a few benign-looking
"camouflage" edges so rings do not sit in trivially separable components.
Ring members share devices (a realistic co-usage signal).

Distribution reference: PaySim (Lopez-Rojas, Elmir & Axelsson, 2016) is used as a
statistical reference for mobile-money transaction shape only — no PaySim data is
included. Every record here is synthetic; ground-truth labels (is_mule_gt,
is_fraud_gt) exist precisely because we control generation.

Output (partitioned Parquet under --out, schemas match BigQuery dataset design §1.2):
    accounts/          account_id, created_at, geo, base_risk, is_mule_gt
    transactions/      txn_id, ts, src_account, dst_account, amount, channel,
                       device_id, is_fraud_gt          (multiple part files)
    ring_assignments/  account_id, ring_id, method='ground_truth', run_id
    manifest.json      parameters, counts, timings

Usage:
    python generator/generate.py --edges 1e6 --out data/1m --seed 42
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# ---------------------------------------------------------------- constants

SIM_START = np.datetime64("2026-04-01T00:00:00")
SIM_START_EPOCH = SIM_START.astype("datetime64[s]").astype(np.int64)

CHANNELS = np.array(["P2P", "MERCHANT", "CASH_IN", "CASH_OUT"])
CHANNEL_P = np.array([0.55, 0.30, 0.08, 0.07])
# log-normal (mu, sigma) per channel — medians ~ e^mu (INR-like scale)
CHANNEL_MU = np.array([6.4, 6.0, 7.6, 7.4])
CHANNEL_SIGMA = np.array([1.10, 1.00, 0.90, 0.90])

GEOS = np.array(["IN-MH", "IN-KA", "IN-DL", "IN-TN", "IN-UP",
                 "IN-GJ", "IN-WB", "IN-TG", "IN-RJ", "IN-HR"])
GEO_P = np.array([0.16, 0.14, 0.13, 0.11, 0.11, 0.09, 0.08, 0.07, 0.06, 0.05])

# diurnal rhythm: low overnight, morning ramp, lunchtime + evening peaks
HOUR_W = np.array([0.5, 0.3, 0.2, 0.15, 0.15, 0.3,
                   0.8, 1.5, 2.2, 2.8, 3.2, 3.5,
                   3.6, 3.3, 3.0, 2.9, 3.0, 3.4,
                   3.8, 4.2, 4.0, 3.2, 2.0, 1.0])
HOUR_P = HOUR_W / HOUR_W.sum()
# weekly rhythm Mon..Sun (sim starts on a Wednesday; indexed by day offset % 7)
DOW_W = np.array([1.0, 1.02, 1.10, 1.25, 1.15, 0.95, 0.98])

# fraud rings injected per 1M target edges (scaled linearly, x --fraud-factor)
RINGS_PER_1M = {"SMURF": 30, "DISPERSE": 25, "CYCLE": 20, "CHAIN": 25, "DORMANT": 15}

SMURF_THRESHOLD = 10_000.0  # structuring threshold the smurfs stay under


# ---------------------------------------------------------------- helpers

def prefixed(prefix: str, ints: np.ndarray) -> np.ndarray:
    """Vectorized 'A123'-style string ids from an int array."""
    return np.char.add(prefix, ints.astype(np.int64).astype("U12"))


def epoch_to_ts(epoch_s: np.ndarray) -> pa.Array:
    return pa.array(epoch_s.astype("datetime64[s]").astype("datetime64[us]"))


def sample_times(rng, n, days, day_p):
    """Timestamps (epoch seconds) with weekly + diurnal rhythm."""
    day = rng.choice(days, size=n, p=day_p)
    hour = rng.choice(24, size=n, p=HOUR_P)
    sec = rng.integers(0, 3600, size=n)
    return SIM_START_EPOCH + day * 86400 + hour * 3600 + sec


def background_devices(rng, src_idx):
    """Deterministic 1-3 device pool per account; sample one per txn."""
    ndev = 1 + (src_idx % 3)
    k = rng.integers(0, 3, size=len(src_idx)) % ndev
    return prefixed("D", src_idx.astype(np.int64) * 4 + k)


# ---------------------------------------------------------------- fraud injector

class FraudInjector:
    """Builds fraud-ring transactions + mule accounts + ground-truth assignments.

    Ring sizes are tiny relative to edge count, so plain-Python assembly is fine;
    everything is converted to numpy arrays at the end.
    """

    def __init__(self, rng, n_bg_accounts, days, day_p):
        self.rng = rng
        self.n_bg = n_bg_accounts
        self.days = days
        self.day_p = day_p
        self.next_account = n_bg_accounts  # fraud accounts appended after background
        self.accounts = []      # (idx, created_epoch_s, geo, base_risk)
        self.rows = []          # (ts, src_idx, dst_idx, amount, channel, device, is_fraud)
        self.assignments = []   # (account_idx, ring_id)
        self.ring_seq = 0
        self.counts = {k: 0 for k in RINGS_PER_1M}

    # -- account/device utilities

    def _new_mule(self, ring_id, age_days_range=(30, 720)):
        rng = self.rng
        idx = self.next_account
        self.next_account += 1
        age = rng.integers(*age_days_range)
        created = SIM_START_EPOCH - int(age) * 86400
        geo = GEOS[rng.integers(0, len(GEOS))]
        self.accounts.append((idx, created, geo, round(float(rng.beta(2, 30)), 4)))
        self.assignments.append((idx, ring_id))
        return idx

    def _ring_device(self, k=0):
        return f"DR{self.ring_seq * 2 + k}"

    def _window_start(self):
        """Ring activity window start: a random day (not at sim edges), daytime hour."""
        rng = self.rng
        day = int(rng.integers(2, self.days - 3))
        hour = int(rng.choice(24, p=HOUR_P))
        return SIM_START_EPOCH + day * 86400 + hour * 3600 + int(rng.integers(0, 3600))

    def _bg_account(self):
        return int(self.rng.integers(0, self.n_bg))

    def _emit(self, ts, src, dst, amount, channel, device, is_fraud=True):
        self.rows.append((int(ts), int(src), int(dst), round(float(amount), 2),
                          channel, device, is_fraud))

    def _camouflage(self, members):
        """2-5 benign-looking edges tying ring members into the background graph."""
        rng = self.rng
        for _ in range(int(rng.integers(2, 6))):
            m = members[int(rng.integers(0, len(members)))]
            bg = self._bg_account()
            src, dst = (m, bg) if rng.random() < 0.5 else (bg, m)
            ts = sample_times(rng, 1, self.days, self.day_p)[0]
            amt = float(np.clip(rng.lognormal(6.2, 1.0), 1, 50_000))
            self._emit(ts, src, dst, amt, "P2P", self._ring_device(0), is_fraud=False)

    # -- topologies

    def smurf_fan_in(self):
        """Many small senders -> one collector, amounts under threshold, then egress."""
        rng = self.rng
        ring_id = f"GT-SMURF-{self.ring_seq:05d}"
        collector = self._new_mule(ring_id)
        senders = [self._new_mule(ring_id) for _ in range(int(rng.integers(12, 41)))]
        t0 = self._window_start()
        total = 0.0
        for s in senders:
            for _ in range(int(rng.integers(1, 4))):
                ts = t0 + int(rng.integers(0, 5 * 86400))          # spread over ~5 days
                amt = float(rng.uniform(3000, SMURF_THRESHOLD * 0.99))
                total += amt
                self._emit(ts, s, collector, amt, "P2P", self._ring_device(rng.integers(0, 2)))
        # collector consolidates proceeds out
        for _ in range(int(rng.integers(1, 3))):
            ts = t0 + int(rng.integers(5, 7) * 86400) + int(rng.integers(0, 43200))
            self._emit(ts, collector, self._bg_account(), total * rng.uniform(0.3, 0.6),
                       "CASH_OUT", self._ring_device(0))
        self._camouflage([collector] + senders)
        self._finish("SMURF")

    def dispersal_fan_out(self):
        """One compromised background source -> many fresh mules within hours -> cash-out."""
        rng = self.rng
        ring_id = f"GT-DISPERSE-{self.ring_seq:05d}"
        source = self._bg_account()                     # victim: existing account, not a mule
        mules = [self._new_mule(ring_id, (5, 120)) for _ in range(int(rng.integers(10, 31)))]
        t0 = self._window_start()
        pot = float(rng.lognormal(11.5, 0.4))           # ~1L INR scale stolen pot
        shares = rng.dirichlet(np.ones(len(mules))) * pot
        for m, amt in zip(mules, shares):
            ts = t0 + int(rng.integers(0, 6 * 3600))                     # within 6 hours
            self._emit(ts, source, m, max(amt, 100.0), "P2P", self._ring_device(0))
            ts2 = ts + int(rng.integers(600, 12 * 3600))                 # quick cash-out
            self._emit(ts2, m, self._bg_account(), max(amt, 100.0) * rng.uniform(0.9, 0.99),
                       "CASH_OUT", self._ring_device(rng.integers(0, 2)))
        self._camouflage(mules)
        self._finish("DISPERSE")

    def cyclic_ring(self):
        """Closed loop of 4-7 mules; value circulates with 1-3% decay per hop."""
        rng = self.rng
        ring_id = f"GT-CYCLE-{self.ring_seq:05d}"
        members = [self._new_mule(ring_id) for _ in range(int(rng.integers(4, 8)))]
        t0 = self._window_start()
        for c in range(int(rng.integers(3, 7))):                          # cycles over ~2 weeks
            ts = t0 + c * int(rng.integers(1, 4)) * 86400
            amt = float(rng.lognormal(10.8, 0.3))                         # ~50k INR scale
            for i in range(len(members)):
                src, dst = members[i], members[(i + 1) % len(members)]
                ts += int(rng.integers(300, 6 * 3600))                    # short dwell
                self._emit(ts, src, dst, amt, "P2P", self._ring_device(rng.integers(0, 2)))
                amt *= float(rng.uniform(0.97, 0.99))                     # fee decay
        self._camouflage(members)
        self._finish("CYCLE")

    def mule_chain(self):
        """Layering path: victim -> M1 -> M2 -> ... -> Mk -> cash-out, short dwell."""
        rng = self.rng
        ring_id = f"GT-CHAIN-{self.ring_seq:05d}"
        chain = [self._new_mule(ring_id) for _ in range(int(rng.integers(4, 9)))]
        t0 = self._window_start()
        for _ in range(int(rng.integers(1, 4))):                          # 1-3 passes
            ts = t0 + int(rng.integers(0, 3 * 86400))
            amt = float(rng.lognormal(11.0, 0.4))
            self._emit(ts, self._bg_account(), chain[0], amt, "P2P", self._ring_device(0))
            for i in range(len(chain) - 1):
                ts += int(rng.integers(300, 7200))                        # 5-120 min dwell
                amt *= float(rng.uniform(0.98, 1.0))                      # mule keeps a cut
                self._emit(ts, chain[i], chain[i + 1], amt, "P2P",
                           self._ring_device(rng.integers(0, 2)))
            ts += int(rng.integers(300, 7200))
            self._emit(ts, chain[-1], self._bg_account(), amt * 0.98, "CASH_OUT",
                       self._ring_device(0))
        self._camouflage(chain)
        self._finish("CHAIN")

    def dormant_burst(self):
        """Aged, silent accounts suddenly activate as a fan-in ring within 48h."""
        rng = self.rng
        ring_id = f"GT-DORMANT-{self.ring_seq:05d}"
        members = [self._new_mule(ring_id, (730, 1460)) for _ in range(int(rng.integers(8, 21)))]
        collector = members[0]
        t0 = self._window_start()
        total = 0.0
        for m in members[1:]:
            for _ in range(int(rng.integers(1, 3))):
                ts = t0 + int(rng.integers(0, 48 * 3600))                 # burst inside 48h
                amt = float(rng.uniform(2000, 15000))
                total += amt
                self._emit(ts, m, collector, amt, "P2P", self._ring_device(rng.integers(0, 2)))
        ts = t0 + int(rng.integers(48, 72)) * 3600
        self._emit(ts, collector, self._bg_account(), total * rng.uniform(0.4, 0.7),
                   "CASH_OUT", self._ring_device(0))
        self._camouflage(members)
        self._finish("DORMANT")

    def _finish(self, pattern):
        self.counts[pattern] += 1
        self.ring_seq += 1

    def inject(self, edges_target, fraud_factor):
        scale = edges_target / 1e6 * fraud_factor
        plan = {p: max(1, round(n * scale)) for p, n in RINGS_PER_1M.items()}
        topo = {"SMURF": self.smurf_fan_in, "DISPERSE": self.dispersal_fan_out,
                "CYCLE": self.cyclic_ring, "CHAIN": self.mule_chain,
                "DORMANT": self.dormant_burst}
        for pattern, n in plan.items():
            for _ in range(n):
                topo[pattern]()
        return plan


# ---------------------------------------------------------------- writers

TXN_SCHEMA = pa.schema([
    ("txn_id", pa.string()),
    ("ts", pa.timestamp("us")),
    ("src_account", pa.string()),
    ("dst_account", pa.string()),
    ("amount", pa.float64()),
    ("channel", pa.string()),
    ("device_id", pa.string()),
    ("is_fraud_gt", pa.bool_()),
])


def write_txn_part(path, part, txn_start, ts, src, dst, amount, channel, device, fraud):
    table = pa.Table.from_arrays([
        pa.array(prefixed("T", txn_start + np.arange(len(ts), dtype=np.int64))),
        epoch_to_ts(ts),
        pa.array(prefixed("A", src)),
        pa.array(prefixed("A", dst)),
        pa.array(amount, type=pa.float64()),
        pa.array(channel, type=pa.string()),
        pa.array(device, type=pa.string()),
        pa.array(fraud, type=pa.bool_()),
    ], schema=TXN_SCHEMA)
    pq.write_table(table, path / f"part-{part:05d}.parquet", compression="snappy")
    return len(ts)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="FraudGraph synthetic transaction-graph generator")
    ap.add_argument("--edges", type=float, required=True, help="target edge count, e.g. 1e6")
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--days", type=int, default=90, help="simulated window length")
    ap.add_argument("--fraud-factor", type=float, default=1.0,
                    help="scales injected ring counts (1.0 = design default)")
    ap.add_argument("--chunk-size", type=int, default=2_000_000)
    args = ap.parse_args()

    t_start = time.time()
    edges = int(args.edges)
    if edges < 100_000:
        ap.error("--edges must be >= 1e5")
    rng = np.random.default_rng(args.seed)
    run_id = f"gen-v1-e{edges}-s{args.seed}"

    out = args.out
    for sub in ("accounts", "transactions", "ring_assignments"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    # -- background account population with power-law activity weights
    n_bg = max(2000, edges // 20)
    day_p = DOW_W[(np.arange(args.days)) % 7]
    day_p = day_p / day_p.sum()

    src_w = rng.pareto(1.16, n_bg) + 1          # senders: 80/20-ish
    dst_w = rng.pareto(1.05, n_bg) + 1          # receivers: heavier tail (merchant hubs)
    src_p, dst_p = src_w / src_w.sum(), dst_w / dst_w.sum()

    # -- fraud first, so the background fills the remaining edge budget exactly
    inj = FraudInjector(rng, n_bg, args.days, day_p)
    plan = inj.inject(edges, args.fraud_factor)
    fraud_rows = inj.rows
    n_fraud_txns = sum(1 for r in fraud_rows if r[6])
    n_background = edges - len(fraud_rows)
    print(f"[gen] target={edges:,} edges | background={n_background:,} | "
          f"ring txns={len(fraud_rows):,} (fraud={n_fraud_txns:,}) | rings={inj.ring_seq}")

    # -- background traffic, chunked
    txn_id = 0
    part = 0
    out_deg = np.zeros(inj.next_account, dtype=np.int64)
    channel_totals = np.zeros(len(CHANNELS), dtype=np.int64)
    remaining = n_background
    while remaining > 0:
        n = min(args.chunk_size, remaining)
        src = rng.choice(n_bg, size=n, p=src_p)
        dst = rng.choice(n_bg, size=n, p=dst_p)
        clash = src == dst
        dst[clash] = (dst[clash] + 1 + rng.integers(0, n_bg - 1, clash.sum())) % n_bg
        ch = rng.choice(len(CHANNELS), size=n, p=CHANNEL_P)
        amount = np.clip(np.round(rng.lognormal(CHANNEL_MU[ch], CHANNEL_SIGMA[ch]), 2),
                         1.0, 500_000.0)
        ts = sample_times(rng, n, args.days, day_p)
        device = background_devices(rng, src)
        write_txn_part(out / "transactions", part, txn_id, ts, src, dst, amount,
                       CHANNELS[ch], device, np.zeros(n, dtype=bool))
        np.add.at(out_deg, src, 1)
        channel_totals += np.bincount(ch, minlength=len(CHANNELS))
        txn_id += n
        part += 1
        remaining -= n
        print(f"[gen] background part-{part - 1:05d}: {n:,} rows "
              f"({txn_id:,}/{n_background:,}, {time.time() - t_start:.0f}s elapsed)")

    # -- fraud part
    f_ts = np.array([r[0] for r in fraud_rows], dtype=np.int64)
    f_src = np.array([r[1] for r in fraud_rows], dtype=np.int64)
    f_dst = np.array([r[2] for r in fraud_rows], dtype=np.int64)
    f_amt = np.array([r[3] for r in fraud_rows], dtype=np.float64)
    f_ch = np.array([r[4] for r in fraud_rows], dtype="U8")
    f_dev = np.array([r[5] for r in fraud_rows], dtype="U16")
    f_fraud = np.array([r[6] for r in fraud_rows], dtype=bool)
    write_txn_part(out / "transactions", part, txn_id, f_ts, f_src, f_dst, f_amt,
                   f_ch, f_dev, f_fraud)
    np.add.at(out_deg, f_src, 1)

    # -- accounts table (background + mule accounts)
    n_total = inj.next_account
    created = SIM_START_EPOCH - rng.integers(1, 1096, size=n_bg) * 86400
    geo = GEOS[rng.choice(len(GEOS), size=n_bg, p=GEO_P)]
    base_risk = np.round(rng.beta(2, 50, size=n_bg), 4)
    is_mule = np.zeros(n_total, dtype=bool)

    acc_created = np.empty(n_total, dtype=np.int64)
    acc_geo = np.empty(n_total, dtype="U8")
    acc_risk = np.empty(n_total, dtype=np.float64)
    acc_created[:n_bg], acc_geo[:n_bg], acc_risk[:n_bg] = created, geo, base_risk
    for idx, c, g, r in inj.accounts:
        acc_created[idx], acc_geo[idx], acc_risk[idx], is_mule[idx] = c, g, r, True

    accounts = pa.Table.from_arrays([
        pa.array(prefixed("A", np.arange(n_total, dtype=np.int64))),
        epoch_to_ts(acc_created),
        pa.array(acc_geo, type=pa.string()),
        pa.array(acc_risk, type=pa.float64()),
        pa.array(is_mule, type=pa.bool_()),
    ], names=["account_id", "created_at", "geo", "base_risk", "is_mule_gt"])
    pq.write_table(accounts, out / "accounts" / "part-00000.parquet", compression="snappy")

    # -- ground-truth ring assignments
    ga_idx = np.array([a for a, _ in inj.assignments], dtype=np.int64)
    assignments = pa.Table.from_arrays([
        pa.array(prefixed("A", ga_idx)),
        pa.array([r for _, r in inj.assignments], type=pa.string()),
        pa.array(np.repeat("ground_truth", len(ga_idx)), type=pa.string()),
        pa.array(np.repeat(run_id, len(ga_idx)), type=pa.string()),
    ], names=["account_id", "ring_id", "method", "run_id"])
    pq.write_table(assignments, out / "ring_assignments" / "part-00000.parquet",
                   compression="snappy")

    # -- manifest + honest summary stats
    total_txns = txn_id + len(fraud_rows)
    top1 = int(max(1, n_total // 100))
    top1_share = float(np.sort(out_deg)[::-1][:top1].sum() / max(out_deg.sum(), 1))
    manifest = {
        "run_id": run_id,
        "seed": args.seed,
        "target_edges": edges,
        "generated_txns": int(total_txns),
        "background_txns": int(txn_id),
        "ring_txns": len(fraud_rows),
        "fraud_txns": int(n_fraud_txns),
        "fraud_rate": round(n_fraud_txns / total_txns, 6),
        "accounts_total": int(n_total),
        "accounts_mule": int(is_mule.sum()),
        "rings": inj.ring_seq,
        "rings_by_pattern": plan,
        "sim_days": args.days,
        "sim_start": str(SIM_START),
        "top1pct_sender_share": round(top1_share, 4),
        "channel_mix": {c: int(n) for c, n in zip(CHANNELS, channel_totals)},
        "elapsed_seconds": round(time.time() - t_start, 1),
        "distribution_reference": "PaySim (Lopez-Rojas, Elmir & Axelsson, 2016) — "
                                  "statistical shape reference only; all data synthetic",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    print(f"[gen] done in {manifest['elapsed_seconds']}s -> {out}")


if __name__ == "__main__":
    main()
