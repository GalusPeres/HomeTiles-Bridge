"""Exercise HA forecast subscriptions with the production Bridge delivery path."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from types import MethodType, SimpleNamespace
import unittest

import test_weather_forecasts as forecasts


class Provider:
    """HA entity API boundary: converted forecasts are delivered to subscribers."""
    def __init__(self, responses, features=6):
        self.responses = responses
        self.supported_features = features
        self.listeners = {}
        self.initial_calls = []

    def async_subscribe_forecast(self, kind, listener):
        self.listeners.setdefault(kind, []).append(listener)
        return lambda: self.listeners[kind].remove(listener)

    async def async_update_listeners(self, kinds):
        for kind in kinds:
            self.initial_calls.append(kind)
            value = self.responses.get(kind)
            if isinstance(value, Exception):
                raise value
            self.emit(kind, value)

    def emit(self, kind, value):
        for listener in tuple(self.listeners.get(kind, [])):
            listener(deepcopy(value))


class WeatherRefreshTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, responses, features=6):
        runtime, state, calls = forecasts.WeatherForecastTests().runtime(responses, features)
        scope = runtime._build_weather_payload.__func__.__globals__
        published = []
        async def publish(hass, topic, payload, **kwargs):
            published.append(json.loads(payload))
        scope.update(mqtt=SimpleNamespace(async_publish=publish))
        runtime._runtime_setup_complete = True
        runtime._weather_pending = set()
        runtime._weather_initial_updates = {}
        runtime._weather_update_task = None
        runtime._unsub_weather_stop = None
        runtime._unsub_weather_started = None
        runtime._weather_last_payload = {}
        runtime._weather_revision = {}
        runtime.weathers = [state.entity_id]
        runtime._owns_state_publish = lambda _: True
        runtime._ha_topic_for_entity = lambda entity, suffix: f"ha/{entity}/{suffix}"
        runtime.hass.async_create_task = lambda coro, **kwargs: asyncio.create_task(coro)
        runtime.hass.is_running = True
        provider = Provider(responses, features)
        runtime.hass.data = {"weather": SimpleNamespace(get_entity=lambda _: provider)}
        for name in ("_weather_started", "_sync_weather_subscriptions", "_subscribe_weather_forecast",
                     "_queue_weather_update", "_async_process_weather_updates",
                     "_async_stop_weather_updates", "_async_publish_weather_state"):
            setattr(runtime, name, MethodType(scope[name], runtime))
        return runtime, state, provider, published, calls, scope

    async def drain(self, runtime):
        if runtime._weather_update_task is not None:
            await runtime._weather_update_task

    async def test_late_forecast_delivered_on_notification_without_state_event_or_timer(self):
        runtime, state, provider, published, calls, scope = self.runtime({})
        runtime._weather_started(runtime.hass)
        await self.drain(runtime)
        self.assertNotIn("forecast", published[-1])
        data = forecasts.yandex_forecasts()
        provider.emit("twice_daily", data["twice_daily"])
        provider.emit("hourly", data["hourly"])
        await self.drain(runtime)
        self.assertEqual(len(published), 2)
        self.assertEqual(len(published[-1]["forecast"]), 7)
        self.assertEqual(len(published[-1]["forecast_hourly"]), 30)
        self.assertFalse(calls, "Subscribed forecasts do not need service polling")
        provider.emit("hourly", data["hourly"])
        await self.drain(runtime)
        self.assertEqual(len(published), 2)
        data["twice_daily"][1]["temperature"] = 31
        provider.emit("twice_daily", data["twice_daily"])
        await self.drain(runtime)
        self.assertEqual(published[-1]["forecast"][0]["temperature"], 31)
        now = scope["dt_util"].utcnow()
        scope["dt_util"].utcnow = lambda: now + timedelta(minutes=11)
        provider.emit("twice_daily", data["twice_daily"])
        provider.emit("hourly", data["hourly"])
        await self.drain(runtime)
        await runtime._async_publish_weather_state(state.entity_id, state, only_if_changed=True)
        self.assertFalse(calls)
        self.assertEqual(len(published), 3)

    async def test_startup_gate_late_entity_and_repeated_sync(self):
        runtime, state, provider, published, _, _ = self.runtime(forecasts.yandex_forecasts())
        runtime.hass.is_running = False
        runtime._sync_weather_subscriptions()
        self.assertFalse(provider.listeners)
        runtime.hass.is_running = True
        runtime._weather_started(runtime.hass)
        await self.drain(runtime)
        self.assertEqual(len(published[-1]["forecast"]), 7)
        runtime._sync_weather_subscriptions()
        await self.drain(runtime)
        self.assertEqual(len(provider.initial_calls), 2)
        runtime.hass.states.get = lambda _: None
        runtime._sync_weather_subscriptions()
        self.assertFalse(any(provider.listeners.values()))
        runtime.hass.states.get = lambda _: state
        runtime._sync_weather_subscriptions()
        await self.drain(runtime)
        self.assertEqual(len(provider.initial_calls), 4)

    async def test_supported_provider_types_and_daily_precedence(self):
        for features in range(8):
            with self.subTest(features=features):
                data = forecasts.yandex_forecasts()
                data["daily"] = [dict(datetime="2026-09-08", temperature=35, templow=5)]
                runtime, state, provider, published, calls, _ = self.runtime(data, features)
                runtime._sync_weather_subscriptions()
                await self.drain(runtime)
                expected = {kind for kind, bit in {"daily":1,"hourly":2,"twice_daily":4}.items() if features & bit}
                self.assertEqual(set(provider.listeners), expected)
                self.assertFalse(calls)
                if features & 1:
                    self.assertEqual(published[-1]["forecast"][0]["temperature"], 35)

    async def test_provider_reload_and_late_old_callback(self):
        data = forecasts.yandex_forecasts()
        runtime, state, provider, published, _, _ = self.runtime(data)
        runtime._sync_weather_subscriptions()
        await self.drain(runtime)
        old_callback = provider.listeners["twice_daily"][0]
        data = deepcopy(data)
        data["twice_daily"][1]["temperature"] = 32
        replacement = Provider(data)
        runtime.hass.data["weather"].get_entity = lambda _: replacement
        runtime._sync_weather_subscriptions()
        await self.drain(runtime)
        self.assertFalse(any(provider.listeners.values()))
        self.assertEqual(published[-1]["forecast"][0]["temperature"], 32)
        old_callback([])
        await self.drain(runtime)
        self.assertEqual(published[-1]["forecast"][0]["temperature"], 32)

    async def test_owner_only_and_takeover_on_next_forecast(self):
        runtime, state, provider, published, _, _ = self.runtime(forecasts.yandex_forecasts())
        runtime._owns_state_publish = lambda _: False
        runtime._sync_weather_subscriptions()
        await self.drain(runtime)
        self.assertFalse(published)
        runtime._owns_state_publish = lambda _: True
        provider.emit("twice_daily", provider.responses["twice_daily"])
        await self.drain(runtime)
        self.assertEqual(len(published), 1)

    async def test_provider_failure_keeps_other_type_and_later_notification_recovers(self):
        data = forecasts.yandex_forecasts()
        periods = data["twice_daily"]
        data["twice_daily"] = RuntimeError("Provider not ready")
        runtime, state, provider, published, _, _ = self.runtime(data)
        runtime._sync_weather_subscriptions()
        await self.drain(runtime)
        self.assertEqual(len(published[-1]["forecast"]), 2)
        provider.emit("twice_daily", periods)
        await self.drain(runtime)
        self.assertEqual(len(published[-1]["forecast"]), 7)

    async def test_stop_cancels_initial_read_and_removes_all_listeners(self):
        runtime, state, provider, published, _, _ = self.runtime({})
        started = asyncio.Event()
        async def blocked(kinds):
            started.set()
            await asyncio.Event().wait()
        provider.async_update_listeners = blocked
        stopped = []
        runtime._unsub_weather_stop = lambda: stopped.append("stop")
        runtime._unsub_weather_started = lambda: stopped.append("start")
        runtime._sync_weather_subscriptions()
        await started.wait()
        await runtime._async_stop_weather_updates()
        self.assertFalse(any(provider.listeners.values()))
        self.assertEqual(stopped, ["stop", "start"])
        self.assertIsNone(runtime._weather_update_task)
        provider.emit("hourly", [])
        self.assertFalse(runtime._weather_pending)
        self.assertFalse(published)

    async def test_stop_listener_does_not_remove_itself_again(self):
        # Issue #15: Home Assistant removes a one-time listener before calling
        # it; removing it again logged "Unable to remove unknown job listener".
        runtime, _, _, _, _, _ = self.runtime({})
        stopped = []
        runtime._unsub_weather_stop = lambda: stopped.append("stop")
        runtime._unsub_weather_started = lambda: stopped.append("start")
        await runtime._async_stop_weather_updates(SimpleNamespace(event_type="homeassistant_stop"))
        self.assertEqual(stopped, ["start"])
        self.assertIsNone(runtime._unsub_weather_stop)

    async def test_new_notification_wins_over_inflight_old_state_publication(self):
        data = forecasts.yandex_forecasts()
        runtime, state, provider, published, _, _ = self.runtime(data)
        runtime._sync_weather_subscriptions()
        await self.drain(runtime)
        build = runtime._build_weather_payload
        started, resume = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            payload = await build(*args)
            started.set()
            await resume.wait()
            return payload
        runtime._build_weather_payload = delayed
        old = asyncio.create_task(runtime._async_publish_weather_state(state.entity_id, state))
        await started.wait()
        runtime._build_weather_payload = build
        data["twice_daily"][1]["temperature"] = 34
        provider.emit("twice_daily", data["twice_daily"])
        await self.drain(runtime)
        resume.set()
        await old
        self.assertEqual(len(published), 2)
        self.assertEqual(published[-1]["forecast"][0]["temperature"], 34)


if __name__ == "__main__":
    unittest.main()
