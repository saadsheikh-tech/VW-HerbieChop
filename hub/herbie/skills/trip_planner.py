"""S5: Smart trip planner. Thinks about the whole day, not just this drive.

* Fuel: projects the tank level over the rest of this journey *plus* what's still planned
  today (e.g. the drive home after work). If you'd drop below the reserve, it recommends a
  station that is actually on the route, and explains why now (rush hour later).
* Coffee: knows your favourite spot and offers it when it's on the way.
* Stops become real stops on the route: the car pulls in, waits (and refuels), continues.
"""
from __future__ import annotations

from datetime import time as dtime

from ..context import ContextSnapshot
from ..sim import TANK_L, level_after, range_m
from .base import Reply, Services, Skill, Suggestion

RESERVE_PCT = 12.0
FUEL_PRICE_EUR = 1.79  # mock price per litre
RUSH_HOURS = ((dtime(7, 0), dtime(9, 30)), (dtime(16, 30), dtime(19, 0)))
RUSH_FACTOR = 1.25  # stop-and-go burns ~25% more


def in_rush(t: dtime) -> bool:
    return any(a <= t <= b for a, b in RUSH_HOURS)


class TripPlanner(Skill):
    name = "trip_planner"
    intents = {"fuel_query", "find_fuel", "coffee"}
    events = {"fuel_low", "stop_reached", "stop_done"}

    # ---- helpers -----------------------------------------------------------
    def plan_km(self, svc: Services) -> tuple[float, float, list[str]]:
        """(km left on this journey, km still planned later today, reasons)."""
        w, j = svc.world, svc.world.journey()
        this_km = (j.length - w.route_pos_m) / 1000 if j else 0.0
        later_km, reasons = 0.0, []
        for leg in getattr(w, "planned_legs", []):
            factor = RUSH_FACTOR if in_rush(leg["at"]) else 1.0
            later_km += leg["km"] * factor
            reasons.append(f"{leg['name']} at {leg['at']:%H:%M}" + (" (rush hour)" if factor > 1 else ""))
        return this_km, later_km, reasons

    def fuel_check(self, ctx: ContextSnapshot, svc: Services, trace: list[str], urgent: bool = False) -> Reply | None:
        w, j = svc.world, svc.world.journey()
        level = w.get("Vehicle.Powertrain.FuelSystem.RelativeLevel")
        this_km, later_km, reasons = self.plan_km(svc)
        end_level = level_after(level, this_km + later_km)
        trace += [f"fuel {level:.0f}% ≈ {range_m(level) / 1000:.0f} km range",
                  f"planned today: {this_km:.1f} km now + {later_km:.1f} km later ({', '.join(reasons) or 'nothing else'})",
                  f"projected level at the end of the day: {end_level:.0f}% (reserve {RESERVE_PCT:.0f}%)"]
        if end_level >= RESERVE_PCT and not urgent:
            return None
        if any(s["kind"] == "fuel" and not s.get("done") for s in w.route_stops):
            trace.append("a fuel stop is already planned")
            return None
        station = j.amenity_ahead("fuel", w.route_pos_m) if j else None
        if not station:
            return Reply(text=f"Fuel's at {level:.0f}%. There's no station left on this route, but we'll make it; "
                              "let's fill up first thing next drive.", skill=self.name, trace=trace, proactive=True)
        ahead_km = (station["at_m"] - w.route_pos_m) / 1000
        rush = any("rush hour" in r for r in reasons)
        where = station["name"] + (f" on {station['address']}" if station.get("address") else "")
        if later_km > 0:
            why = (f"Heads-up: at this rate you'll run low on your way back home. You'd be down to about "
                   f"{max(end_level, 0):.0f}%{', and that drive is right in rush hour' if rush else ''}. ")
        else:
            why = f"Fuel's getting low: {level:.0f}%, about {range_m(level) / 1000:.0f} km left. "
        s = Suggestion.new(skill=self.name, kind="stop", subject="fuel_stop", payload={"station": station})
        return Reply(text=f"{why}I'd fuel up now: {where} is right on our route, {ahead_km:.1f} km ahead, "
                          "and it only adds a few minutes. Shall I add the stop?",
                     skill=self.name, trace=trace + [f"station on route at {station['at_m']} m"], suggestion=s,
                     proactive=True, data={"station": station})

    def offer_coffee(self, ctx: ContextSnapshot, svc: Services, trace: list[str], ask: bool = True) -> Reply | None:
        w, j = svc.world, svc.world.journey()
        if not j:
            return None
        fav = ctx.user.preferences.get("favourite_cafe", "Starbucks")
        cafes = [a for a in j.amenities if a["kind"] == "cafe" and a["at_m"] >= w.route_pos_m + 250]
        if not cafes:
            return None
        is_fav = lambda c: bool(c.get("address")) and c["address"] in fav  # noqa: E731
        cafe = next((c for c in cafes if is_fav(c)), cafes[0])
        where = cafe["name"] + (f" at {cafe['address']}" if cafe.get("address") else "")
        trace.append(f"favourite café from memory: {fav}; on route at {cafe['at_m']} m")
        s = Suggestion.new(skill=self.name, kind="stop", subject="coffee_stop", payload={"cafe": cafe})
        lead = "Your favourite" if is_fav(cafe) else "There's a"
        return Reply(text=f"{lead} {where} is right on the way and only adds about 3 minutes. Shall I stop there?",
                     skill=self.name, trace=trace, suggestion=s, proactive=True, data={"cafe": cafe})

    # ---- events ------------------------------------------------------------
    def on_event(self, event: str, payload: dict, ctx: ContextSnapshot, svc: Services) -> list[Reply]:
        if event == "fuel_low":
            r = self.fuel_check(ctx, svc, [f"fuel dropped below {payload['threshold']:.0f}%"], urgent=True)
            if r is None:
                planned = next((s for s in svc.world.route_stops if s["kind"] == "fuel" and not s.get("done")), None)
                if planned:
                    return [Reply(text=f"Fuel's down to {payload['level']:.0f}%. Don't worry, we're stopping at "
                                       f"{planned['name']} shortly.", skill=self.name, proactive=True)]
                return []
            return [r]
        kind = payload.get("kind")
        if event == "stop_reached":
            if kind == "fuel":
                return [Reply(text=f"Pulling into {payload['name']}. I'll fill her up and pay from the car "
                                   "(simulated). Sit tight.", skill=self.name, proactive=True)]
            return [Reply(text=f"Here's {payload['name']}. Grab your coffee, I'll wait right here.",
                          skill=self.name, proactive=True)]
        if event == "stop_done":
            if kind == "fuel":
                litres = (100 - payload.get("level_before", 20)) / 100 * TANK_L
                svc.memory.add_episode(ctx.user.user_id, ctx.environment.time, "fuel_stop", payload["name"], None,
                                       "done", {"litres": round(litres, 1)})
                later_rush = any(in_rush(leg["at"]) for leg in svc.world.planned_legs) and not in_rush(ctx.environment.time.time())
                extra = " That covers the drive home too, so no fuel stop in rush hour." if later_rush else ""
                return [Reply(text=f"Full tank: {litres:.0f} litres for about €{litres * FUEL_PRICE_EUR:.0f}.{extra} "
                                   "Back on the road!",
                              skill=self.name, proactive=True)]
            return [Reply(text="Enjoy the coffee! Off we go.", skill=self.name, proactive=True)]
        return []

    # ---- intents -----------------------------------------------------------
    def handle(self, intent, ctx: ContextSnapshot, svc: Services) -> Reply:
        w = svc.world
        level = w.get("Vehicle.Powertrain.FuelSystem.RelativeLevel")
        if intent.name == "coffee":
            r = self.offer_coffee(ctx, svc, ["driver asked for coffee"])
            return r or Reply(text="No café left on this route, sorry. I'll remember you wanted one.", skill=self.name)
        trace: list[str] = []
        r = self.fuel_check(ctx, svc, trace, urgent=intent.name == "find_fuel")
        if r:
            r.proactive = False
            return r
        this_km, later_km, _ = self.plan_km(svc)
        return Reply(text=f"Fuel's at {level:.0f}%, about {range_m(level) / 1000:.0f} km of range. "
                          f"That covers today's {this_km + later_km:.0f} km with room to spare.",
                     skill=self.name, trace=trace)

    def on_feedback(self, s: Suggestion, accepted: bool, ctx: ContextSnapshot, svc: Services) -> Reply:
        w = svc.world
        if s.kind == "question" and s.subject == "had_coffee":
            svc.memory.add_episode(ctx.user.user_id, ctx.environment.time, "coffee_check", None, None,
                                   "had" if accepted else "not_yet")
            if accepted:
                return Reply(text="Great, you're fuelled up then. Now let's see about me.", skill=self.name)
            r = self.offer_coffee(ctx, svc, ["no coffee yet"])
            if r:
                r.text = "Let's fix that. " + r.text
                r.proactive = False
                return r
            return Reply(text="No café on our way, sorry. Water's in the door pocket.", skill=self.name)
        if not accepted:
            svc.memory.add_episode(ctx.user.user_id, ctx.environment.time, "suggestion", s.subject, None, "declined")
            return Reply(text="Okay, no stop. I'll keep an eye on it.", skill=self.name)
        if s.subject == "fuel_stop":
            st = s.payload["station"]
            w.add_route_stop({"kind": "fuel", "name": st["name"], "at_m": st["at_m"], "dwell_s": 8.0,
                              "level_before": w.get("Vehicle.Powertrain.FuelSystem.RelativeLevel")})
            svc.memory.add_episode(ctx.user.user_id, ctx.environment.time, "suggestion", "fuel_stop", None, "accepted")
            return Reply(text=f"Done, {st['name']} is on the route. I'll pull in when we get there.", skill=self.name,
                         trace=[f"route stop added at {st['at_m']} m"])
        cafe = s.payload["cafe"]
        w.add_route_stop({"kind": "cafe", "name": cafe["name"], "at_m": cafe["at_m"], "dwell_s": 10.0})
        svc.memory.set_preference(ctx.user.user_id, "favourite_cafe", f"{cafe['name']} {cafe.get('address') or ''}".strip())
        usual = ctx.user.preferences.get("favourite_coffee")
        return Reply(text=f"Coffee stop added: {cafe['name']}" + (f" at {cafe['address']}" if cafe.get("address") else "")
                          + (f". Your usual {usual}?" if usual else "."), skill=self.name,
                     trace=[f"route stop added at {cafe['at_m']} m; favourite café remembered"])
