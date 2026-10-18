# NEKO-derived bounded affect

Upstream: https://github.com/Project-N-E-K-O/N.E.K.O
Revision: 8d767935f99c31b9b62b9c360d5c181bd48a7d16
Source: main_logic/activity/master_emotion.py:62-102
Adapted locations: agent/presence_shadow.py::AffectReading and _clamp.

Retained original Apache-2.0 attribution and verbatim LICENSE/NOTICE.
Clover modifications remove Focus/model/global state, inject time and bind
readings to scoped explicit thoughts. No NEKO assets, dependencies or models
are included. New thought/initiative behavior is Clover design, not an upstream
persistent companion-emotion implementation. See module modification notices.
