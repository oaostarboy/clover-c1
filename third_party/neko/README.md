# NEKO-derived bounded affect

Upstream: https://github.com/Project-N-E-K-O/N.E.K.O
Revision: 8d767935f99c31b9b62b9c360d5c181bd48a7d16
Source: main_logic/activity/master_emotion.py:62-102
Adapted locations: agent/presence_shadow.py::AffectReading and _clamp;
agent/presence_state.py::AffectReading and AffectState (latest/TTL/sequence reset,
upstream lines 62-102,190-281). The integration host and durable ownership tables
are original Clover code. No additional NEKO code is copied by the integration.

Distribution: pyproject.toml declares this README, LICENSE and NOTICE as license
files, including sealed Nix wheel metadata. Source distributions and the shell
installer's Git checkout retain this directory. Native Nix/Docker image inclusion
still requires the release packaging lane; declaration is not a built-image proof.

Retained original Apache-2.0 attribution and verbatim LICENSE/NOTICE.
Clover modifications remove Focus/model/global state, inject time and bind
readings to scoped authenticated current human evidence. Bounded posture enums,
current-turn extraction/schema prompts, and later-context lexical admission are
original Clover additions, not copied NEKO prompts or algorithms. No NEKO assets,
dependencies or models
are included. New thought/initiative behavior is Clover design, not an upstream
persistent companion-emotion implementation. See module modification notices.
