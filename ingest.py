"""Sensor ingestion abstraction. SENSOR_SOURCE env var ("sim" by default) picks where samples
come from. "serial"/"replay" are stubbed until the real USB rig's data format is known - swap
them in behind these same two functions later; nothing in health.py or watcher_agent.py needs to
change when that happens."""
import os
from sim import synth

SENSOR_SOURCE = os.environ.get("SENSOR_SOURCE", "sim")


def read_baseline(fs: int, seconds: float = 60.0):
    """One-time healthy recording used to fit the baseline."""
    if SENSOR_SOURCE == "sim":
        return synth(fs, seconds, fault=0.0, seed=1)
    if SENSOR_SOURCE == "serial":
        raise NotImplementedError("serial ingest not wired yet - needs the rig's real data format")
    if SENSOR_SOURCE == "replay":
        raise NotImplementedError("replay ingest not wired yet - needs a recorded file format")
    raise ValueError(f"unknown SENSOR_SOURCE: {SENSOR_SOURCE}")


def read_chunk(fs: int, seconds: float, tick: int, fault: float = 0.0):
    """Per-tick chunk of samples to score against the baseline."""
    if SENSOR_SOURCE == "sim":
        return synth(fs, seconds, fault=fault, seed=10 + tick)
    if SENSOR_SOURCE == "serial":
        raise NotImplementedError("serial ingest not wired yet - needs the rig's real data format")
    if SENSOR_SOURCE == "replay":
        raise NotImplementedError("replay ingest not wired yet - needs a recorded file format")
    raise ValueError(f"unknown SENSOR_SOURCE: {SENSOR_SOURCE}")
