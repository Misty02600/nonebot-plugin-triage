from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Protocol

from nbtriage.incident_queries import IncidentQueryService
from nbtriage.live_incidents import LiveIncidentBuffer
from nbtriage.live_trials import LiveTrialService
from nbtriage.message_references import PlatformMessageReferenceIndex
from nbtriage.rate_limits import KeyedRateLimiter
from nbtriage.runtime_observations import RuntimeObservationBuffer
from nonebot_plugin_triage.capability_shadow import (
    CapabilityShadowService,
    register_capability_shadow,
)
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.live_reports import LiveReportService
from nonebot_plugin_triage.model_runtime import NBTriageModelService, create_model_service
from nonebot_plugin_triage.nonebot_runtime import NoneBotRuntimeObserver
from nonebot_plugin_triage.trials import create_trial_service
from nonebot_plugin_triage.universal_references import UniversalReferenceBridge


class OutgoingReferenceProvider(Protocol):
    def register(self) -> None: ...


def _create_outgoing_reference_providers(
    bridge: UniversalReferenceBridge,
) -> tuple[OutgoingReferenceProvider, ...]:
    providers: list[OutgoingReferenceProvider] = []
    try:
        onebot_available = find_spec("nonebot.adapters.onebot.v11") is not None
    except ModuleNotFoundError:
        onebot_available = False
    if onebot_available:
        from nonebot_plugin_triage.onebot_v11_references import (
            OneBotV11OutgoingReferenceProvider,
        )

        providers.append(OneBotV11OutgoingReferenceProvider(bridge))
    return tuple(providers)


@dataclass(frozen=True)
class NBTriagePluginRuntime:
    observer: NoneBotRuntimeObserver
    reference_bridge: UniversalReferenceBridge
    outgoing_reference_providers: tuple[OutgoingReferenceProvider, ...]
    support_rate_limiter: KeyedRateLimiter
    report_service: LiveReportService
    query_service: IncidentQueryService
    incidents: LiveIncidentBuffer
    trials: LiveTrialService
    model_service: NBTriageModelService | None
    capability_shadow: CapabilityShadowService | None


def create_plugin_runtime(
    config: NBTriageConfig,
    *,
    model_service_factory: Callable[
        [NBTriageConfig], NBTriageModelService | None
    ] = create_model_service,
    trial_service_factory: Callable[[NBTriageConfig], LiveTrialService] = (create_trial_service),
) -> NBTriagePluginRuntime:
    runtime_buffer = RuntimeObservationBuffer(
        max_entries=config.nbtriage_observation_max_entries,
        retention_seconds=config.nbtriage_observation_retention_seconds,
    )
    observer = NoneBotRuntimeObserver(runtime_buffer)
    reference_index = PlatformMessageReferenceIndex(
        secret_key=secrets.token_bytes(32),
        max_entries=config.nbtriage_reference_max_entries,
        retention_seconds=config.nbtriage_reference_retention_seconds,
    )
    reference_bridge = UniversalReferenceBridge(reference_index)
    incident_buffer = LiveIncidentBuffer(
        max_entries=config.nbtriage_incident_max_entries,
        retention_seconds=config.nbtriage_incident_retention_seconds,
    )
    trial_service = trial_service_factory(config)
    support_rate_limiter = KeyedRateLimiter(
        secret_key=secrets.token_bytes(32),
        max_scopes=config.nbtriage_rate_limit_max_scopes,
        cooldown_seconds=config.nbtriage_support_cooldown_seconds,
    )
    report_rate_limiter = KeyedRateLimiter(
        secret_key=secrets.token_bytes(32),
        max_scopes=config.nbtriage_rate_limit_max_scopes,
        cooldown_seconds=config.nbtriage_report_cooldown_seconds,
    )
    report_service = LiveReportService(
        reference_bridge=reference_bridge,
        runtime_buffer=runtime_buffer,
        incident_buffer=incident_buffer,
        rate_limiter=report_rate_limiter,
        evidence_retention_seconds=config.nbtriage_observation_retention_seconds,
        trial_service=trial_service,
    )
    query_service = IncidentQueryService(incident_buffer)
    model_service = model_service_factory(config)
    observer.register()
    reference_bridge.register()
    outgoing_reference_providers = _create_outgoing_reference_providers(reference_bridge)
    for provider in outgoing_reference_providers:
        provider.register()
    capability_shadow = register_capability_shadow(config)
    return NBTriagePluginRuntime(
        observer=observer,
        reference_bridge=reference_bridge,
        outgoing_reference_providers=outgoing_reference_providers,
        support_rate_limiter=support_rate_limiter,
        report_service=report_service,
        query_service=query_service,
        incidents=incident_buffer,
        trials=trial_service,
        model_service=model_service,
        capability_shadow=capability_shadow,
    )


__all__ = ("NBTriagePluginRuntime", "create_plugin_runtime")
