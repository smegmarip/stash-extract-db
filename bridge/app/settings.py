from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    stash_url: str = "http://host.docker.internal:9999"
    stash_api_key: str = ""
    stash_session_cookie: str = ""
    extractor_url: str = "http://extractor-gateway:12000"
    data_dir: str = "/data"
    log_level: str = "INFO"

    # Featurization lifecycle (MULTI_CHANNEL_SCORING.md §4).
    # When false, the bridge falls back to on-demand caching against
    # image_hashes (Phase 2 dual-write still active) — used as the rollback
    # path for Phase 3+. When true, /match endpoints gate on job_feature_state
    # and return 503 + Retry-After until a job's features are 'ready'.
    bridge_lifecycle_enabled: bool = False
    bridge_featurize_concurrency: int = 4
    # Stale-task timeout: a 'featurizing' row whose started_at is older than
    # this gets reset on startup. 10 minutes is generous for typical jobs.
    bridge_stale_task_ms: int = 10 * 60 * 1000

    # Featurization parameters. In Phase 3 the bridge picks one algorithm
    # for its server-triggered featurization runs; matching requests still
    # use whatever the scraper sends (their results land in image_features
    # via Phase 2 dual-write, but may use a different algorithm). Phase 4
    # unifies these so featurization is request-driven.
    bridge_featurize_algorithm: str = "phash"
    bridge_featurize_hash_size: int = 8
    # c_i smoothing per §4.6 — used in featurization until Phase 4 hands
    # this off to the scraper config. The "global" values below are the
    # historical single setting; per-channel overrides (added in
    # architectural fix Run 7) take precedence when set.
    bridge_featurize_uniqueness_alpha: float = 1.0
    bridge_featurize_uniqueness_threshold: float = 0.85
    # Per-channel overrides. None = inherit the global value. Mechanism
    # exists for users whose corpus benefits from per-channel tuning;
    # defaults all None because empirical calibration on the Pexels
    # corpus (CALIBRATION_RESULTS.md Run 7) showed the global 0.85 / 1.0
    # is correct for both pHash and tone — counterintuitively, tone's
    # global threshold of 0.85 effectively silences a noisy channel via
    # c_i collapse, which is the desired behavior on that corpus. A
    # corpus where tone is a stronger discriminator (e.g., monochrome
    # film, surveillance footage) might benefit from a stricter tone
    # threshold; that's now reachable without code changes.
    bridge_featurize_uniqueness_threshold_phash: Optional[float] = None
    bridge_featurize_uniqueness_threshold_tone: Optional[float] = None
    bridge_featurize_uniqueness_alpha_phash: Optional[float] = None
    bridge_featurize_uniqueness_alpha_tone: Optional[float] = None

    def channel_uniqueness_threshold(self, channel: str) -> float:
        per = {
            "phash": self.bridge_featurize_uniqueness_threshold_phash,
            "tone":  self.bridge_featurize_uniqueness_threshold_tone,
        }.get(channel)
        return per if per is not None else self.bridge_featurize_uniqueness_threshold

    def channel_uniqueness_alpha(self, channel: str) -> float:
        per = {
            "phash": self.bridge_featurize_uniqueness_alpha_phash,
            "tone":  self.bridge_featurize_uniqueness_alpha_tone,
        }.get(channel)
        return per if per is not None else self.bridge_featurize_uniqueness_alpha
    # Per-job parallel asset fetches inside featurize_task (§4.4).
    bridge_featurize_per_job_concurrency: int = 8

    # Phase 4 new scoring formula. When false, image scoring uses the
    # legacy top-K-mean (§3.2 prior). When true, requests must include
    # image_gamma, image_count_k, image_uniqueness_alpha or the bridge
    # returns 400.
    bridge_new_scoring_enabled: bool = False

    # Phase 6 LRU eviction for Stash-side image_features rows. The
    # extractor side is bounded by job count and cleared on cascade, so
    # only Stash-side rows accumulate without bound. Default budget 1 GB,
    # checked every hour. Set budget to 0 to disable eviction entirely.
    bridge_stash_feature_budget_bytes: int = 1024 * 1024 * 1024
    bridge_lru_eviction_interval_s: int = 3600

    # Phase 7 soft retirement of the legacy image_hashes table. When true
    # (default), pHash compute writes to BOTH image_hashes and
    # image_features (Phase 2 behavior); reads check image_features first
    # and fall back to image_hashes. Flip to false after a stable period
    # of all reads coming from image_features — this stops the dual write
    # and the legacy fallback. The actual `DROP TABLE image_hashes` is a
    # manual operation the user runs once they're confident in the new
    # path; see HOW_TO_USE.md §10.
    bridge_legacy_dual_write_enabled: bool = True

    class Config:
        env_prefix = ""
        case_sensitive = False


settings = Settings()
