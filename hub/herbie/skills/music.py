"""Music: a simulated player that learns taste. No audio is played; tracks are fictional.

The playlist signal (set by personalization, mood reactions or the driver) decides the
genres; liked/disliked genres in memory reorder and filter them. Feedback like "I don't
like this, but recommend me something else" skips the track, remembers the dislike and
switches to a genre the driver has liked before.
"""
from __future__ import annotations

from ..context import ContextSnapshot
from ..personality import human_join
from .base import Reply, Services, Skill, Suggestion, load_json

P_PLAYLIST = "Vehicle.Cabin.Infotainment.Media.Playlist"
P_TRACK = "Vehicle.Cabin.Infotainment.Media.Track"


class Music(Skill):
    name = "music"
    intents = {"music_like", "music_dislike", "music_next", "music_recommend", "music_now", "music_calm", "music_party"}
    events = {"playlist_changed", "track_end"}

    def __init__(self) -> None:
        data = load_json("music.json")
        self.playlists: dict[str, list[str]] = data["playlists"]
        self.tracks: list[dict] = data["tracks"]
        self.queue: list[str] = []
        self.history: list[str] = []

    # ---- player ------------------------------------------------------------
    def taste(self, svc: Services, ctx: ContextSnapshot) -> tuple[list[str], list[str]]:
        p = svc.memory.get_preferences(ctx.user.user_id)
        return p.get("liked_genres", []), p.get("disliked_genres", [])

    def build_queue(self, genres: list[str], svc: Services, ctx: ContextSnapshot) -> list[str]:
        liked, disliked = self.taste(svc, ctx)
        allowed = [g for g in genres if g not in disliked] or genres
        allowed.sort(key=lambda g: (g not in liked, genres.index(g)))
        recent = set(self.history[-6:])
        q = [t["id"] for g in allowed for t in self.tracks if t["genre"] == g and t["id"] not in recent]
        return q or [t["id"] for g in allowed for t in self.tracks if t["genre"] == g]

    def track(self, tid: str | None) -> dict | None:
        return next((t for t in self.tracks if t["id"] == tid), None)

    def play_next(self, svc: Services, ctx: ContextSnapshot) -> dict | None:
        if not self.queue:
            genres = self.playlists.get(svc.world.get(P_PLAYLIST), ["pop", "indie"])
            self.queue = self.build_queue(genres, svc, ctx)
        if not self.queue:
            return None
        t = self.track(self.queue.pop(0))
        self.history.append(t["id"])
        svc.world.signals[P_TRACK] = t
        svc.world.track_elapsed_s = 0.0
        return t

    def current(self, svc: Services) -> dict | None:
        return svc.world.get(P_TRACK)

    def set_genres(self, genres: list[str], svc: Services, ctx: ContextSnapshot) -> dict | None:
        self.queue = self.build_queue(genres, svc, ctx)
        return self.play_next(svc, ctx)

    def remember(self, svc: Services, ctx: ContextSnapshot, genre: str, liked: bool) -> None:
        likes, dislikes = self.taste(svc, ctx)
        add, remove = (likes, dislikes) if liked else (dislikes, likes)
        if genre not in add:
            add.append(genre)
        if genre in remove:
            remove.remove(genre)
        svc.memory.set_preference(ctx.user.user_id, "liked_genres", likes)
        svc.memory.set_preference(ctx.user.user_id, "disliked_genres", dislikes)
        svc.memory.add_episode(ctx.user.user_id, ctx.environment.time, "music_feedback", genre, None,
                               "liked" if liked else "disliked")

    def similar(self, t: dict, svc: Services, ctx: ContextSnapshot) -> list[dict]:
        return [x for x in self.tracks if x["genre"] == t["genre"] and x["id"] != t["id"]][:3]

    # ---- events ------------------------------------------------------------
    def on_event(self, event: str, payload: dict, ctx: ContextSnapshot, svc: Services) -> list[Reply]:
        if event == "playlist_changed":
            self.queue = []
        self.play_next(svc, ctx)
        return []  # track changes are silent; the screen shows them

    # ---- intents -----------------------------------------------------------
    def handle(self, intent, ctx: ContextSnapshot, svc: Services) -> Reply:
        t = self.current(svc)
        n = intent.name
        if n == "music_now":
            if not t:
                return Reply(text="Nothing's playing right now. Want some music?", skill=self.name)
            return Reply(text=f"This is {t['title']} by {t['artist']}, a bit of {t['genre']}.", skill=self.name)
        if n == "music_calm":
            svc.world.set(P_PLAYLIST, "Calm Drive")
            t = self.set_genres(self.playlists["Calm Drive"], svc, ctx)
            return Reply(text=f"Something calmer then: {t['title']} by {t['artist']}.", skill=self.name,
                         trace=["playlist → Calm Drive"])
        if n == "music_party":
            svc.world.set(P_PLAYLIST, "Friday Night")
            t = self.set_genres(self.playlists["Friday Night"], svc, ctx)
            return Reply(text=f"Turning it up: {t['title']} by {t['artist']}!", skill=self.name, trace=["playlist → Friday Night"])
        if not t:
            t = self.play_next(svc, ctx)
            return Reply(text=f"Let's start with {t['title']} by {t['artist']}.", skill=self.name)
        if n == "music_like":
            self.remember(svc, ctx, t["genre"], True)
            sims = self.similar(t, svc, ctx)
            self.queue = [s["id"] for s in sims] + self.queue
            return Reply(text=f"Noted, you like {t['genre']}. I've queued similar tracks: "
                              f"{human_join([s['title'] for s in sims])}.",
                         skill=self.name, trace=[f"liked genre '{t['genre']}' saved to memory"],
                         data={"recommendations": sims})
        if n == "music_dislike":
            self.remember(svc, ctx, t["genre"], False)
            liked, disliked = self.taste(svc, ctx)
            options = [g for g in liked if g not in disliked] or [
                g for g in ("indie", "jazz", "funk", "pop", "lofi") if g not in disliked]
            nxt = self.set_genres(options, svc, ctx)
            recs = [self.track(i) for i in self.queue[:3]]
            return Reply(text=f"Fair enough, less {t['genre']} from now on. Try this instead: {nxt['title']} by "
                              f"{nxt['artist']} ({nxt['genre']}). Coming up after that: "
                              f"{human_join([r['title'] for r in recs if r])}.",
                         skill=self.name, trace=[f"disliked genre '{t['genre']}' saved; switched to {options[:3]}"],
                         data={"recommendations": recs})
        if n == "music_recommend":
            liked, _ = self.taste(svc, ctx)
            genres = liked or [t["genre"]]
            recs = [x for x in self.tracks if x["genre"] in genres and x["id"] != t["id"]][:3]
            s = Suggestion.new(skill=self.name, kind="music", subject="recommendations", payload={"ids": [r["id"] for r in recs]})
            basis = f"you liked {human_join(liked)}" if liked else f"you're listening to {t['genre']}"
            listing = human_join([r["title"] + " by " + r["artist"] for r in recs])
            return Reply(text=f"Since {basis}: {listing}. Want me to queue them?",
                         skill=self.name, suggestion=s, data={"recommendations": recs})
        nxt = self.play_next(svc, ctx)  # music_next
        return Reply(text=f"Skipping. Here's {nxt['title']} by {nxt['artist']}.", skill=self.name)

    def on_feedback(self, s: Suggestion, accepted: bool, ctx: ContextSnapshot, svc: Services) -> Reply:
        if not accepted:
            return Reply(text="No problem, I'll keep the current mix.", skill=self.name)
        if s.subject == "more_like_this":
            t = self.current(svc)
            if t:
                self.remember(svc, ctx, t["genre"], True)
                sims = self.similar(t, svc, ctx)
                self.queue = [x["id"] for x in sims] + self.queue
                return Reply(text=f"More {t['genre']} coming up: {human_join([x['title'] for x in sims])}.",
                             skill=self.name, data={"recommendations": sims}, trace=["liked genre saved to memory"])
            return Reply(text="Okay!", skill=self.name)
        self.queue = list(s.payload.get("ids", [])) + self.queue
        return Reply(text="Queued. Enjoy!", skill=self.name)
