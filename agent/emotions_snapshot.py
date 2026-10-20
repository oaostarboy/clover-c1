"""Read-only revision observations for one local /emotions projection.

This is optimistic validation, not a transaction across settings and two DBs.
Keep each SQLite observer open: data_version is connection-relative and detects
commits even when values were changed back (including WAL writes). File identity
and ctime fence replacement and settings edits. No retry, repair or write lock.
seal() finishes bounded projections before the revision probes; current() is
strictly in-memory. Session bookkeeping is not an authority revision.
"""
from pathlib import Path
import sqlite3


def _signature(path):
    try:
        st = path.stat()
        link = path.lstat()
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError('dangling status source')
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            link.st_dev, link.st_ino, link.st_mtime_ns, link.st_ctime_ns)


class ReadFence:
    """Capture authority before reads, seal observations, then evaluate once.

    Trusted owners must invalidate/replace their sample or host on revocation;
    gateway_current is a local non-blocking predicate, never network/auth refresh.
    External stores are observed through the final revision probes, not locked
    until physical delivery. Changes after those observations belong to the next
    request. Do not call an I/O helper after current().
    """
    def __init__(self, db, *, session_id=None, authority_paths=()):
        from clover_constants import get_clover_home
        from clover_cli.managed_scope import get_managed_dir
        self.home_setting = get_clover_home()
        self.home = self.home_setting.resolve()
        managed = get_managed_dir()
        self.managed = managed
        self.paths = [self.home / 'config.yaml', self.home / 'cron' / 'presence.db', *authority_paths]
        if managed is not None:
            self.paths.append(managed / 'config.yaml')
        # Also detect a previously absent managed directory becoming effective.
        from clover_cli.managed_scope import _DEFAULT_MANAGED_DIR
        import os
        configured = os.environ.get('CLOVER_MANAGED_DIR', '').strip()
        if configured:
            self.paths.append(Path(configured))
        elif 'PYTEST_CURRENT_TEST' not in os.environ:
            self.paths.append(_DEFAULT_MANAGED_DIR)
        if db is not None:
            path = Path(db.db_path).resolve()
            if path != self.home / 'state.db':
                raise ValueError('foreign status database')
            self.paths.append(path)
        self.session_id = session_id
        self.session_conn = None
        self.session_before = None
        self.source_before = None
        self.source_ref = None
        self.connections = []
        self.bound = None
        self.sealed = False
        self.unchanged = False
        self.gateway_valid = False
        self.before = tuple(self._file_revision(p) for p in self.paths)
        try:
            for path in self.paths:
                if path.name not in {'presence.db', 'state.db'} or not path.exists():
                    continue
                conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
                self.connections.append((conn, conn.execute('PRAGMA data_version').fetchone()[0]))
                if path.name == 'state.db':
                    self.session_conn = conn
                    self.session_before = self._lineage(conn, session_id)
        except Exception:
            self.close()
            raise

    def bind(self, key, owner):
        from agent import emotions
        if owner is None:
            return
        host_ref = emotions._owner_hosts.get(owner)
        host = host_ref() if host_ref else None
        if host is None:
            raise ValueError('missing status host')
        if owner._latest is not None:
            self.source_ref = owner._latest.source
            self.source_before = self._source(self.session_conn, self.source_ref)
        self.bound = (key, emotions._owners.get(key), owner, owner._latest,
                      owner._generation, owner._ticket, host_ref, host,
                      host.state, host.key, host.original_key, host.reader, host.store,
                      host.input_generation, host.epoch, host.stop_generation,
                      host.gateway_current, self._agent_binding(host.agent))

    @staticmethod
    def _file_revision(path):
        value = _signature(path)
        # Session token accounting/title updates are not disclosure authority.
        # Only replacement is a file-level revocation; exact row revisions below
        # fence nominated evidence/receipts/lineage, never the whole transcript.
        return value[:2] + value[5:7] if value is not None and path.name == 'state.db' else value

    @staticmethod
    def _lineage(conn, session_id):
        if conn is None or session_id is None:
            return ()
        result, seen = [], set()
        while session_id and session_id not in seen and len(seen) < 100:
            seen.add(session_id)
            row = conn.execute('SELECT parent_session_id,source FROM sessions WHERE id=?', (session_id,)).fetchone()
            result.append((session_id, row))
            session_id = row[0] if row else None
        return tuple(result)

    @classmethod
    def _source(cls, conn, source):
        import json
        if conn is None:
            raise ValueError('missing evidence database')
        session_id, row_id = json.loads(source.source_id)
        row = conn.execute('SELECT role,active,tool_call_id,content,display_kind FROM messages WHERE session_id=? AND id=?',
                           (session_id, row_id)).fetchone()
        receipt = conn.execute('SELECT * FROM human_origin_receipts WHERE session_id=? AND message_row_id=?',
                               (session_id, row_id)).fetchone()
        return (row, receipt, cls._lineage(conn, session_id))

    @staticmethod
    def _agent_binding(agent):
        return (agent, getattr(agent, '_human_ingress_stop_generation', 0),
                getattr(agent, 'platform', ''), getattr(agent, '_chat_type', ''),
                getattr(agent, '_chat_id', ''), getattr(agent, '_user_id', ''))

    def close(self):
        for conn, _ in self.connections:
            conn.close()
        self.connections.clear()

    def seal(self):
        """Finish exact authority projections, then observe storage revisions."""
        from clover_constants import get_clover_home
        from clover_cli.managed_scope import get_managed_dir
        try:
            self.gateway_valid = self.bound is None or self.bound[7].gateway_current() is True
            same = (get_clover_home().resolve() == self.home and get_managed_dir() == self.managed)
            rows_same, source_version = True, None
            if self.session_conn is not None:
                source_version = self.session_conn.execute('PRAGMA data_version').fetchone()[0]
                rows_same = self._lineage(self.session_conn, self.session_id) == self.session_before
                if self.source_ref is not None:
                    rows_same = self._source(self.session_conn, self.source_ref) == self.source_before and rows_same
            # No substantive source reads after this point, across EITHER DB.
            # Final version probes follow all projections, including cross-store
            # mutations during a source read on a different WAL connection.
            versions = [conn.execute('PRAGMA data_version').fetchone()[0] ==
                        (source_version if conn is self.session_conn else version)
                        for conn, version in self.connections]
            versions.append(rows_same)
            self.close()
            after = tuple(self._file_revision(p) for p in self.paths)
            self.unchanged = same and all(versions) and after == self.before
            self.sealed = True
        finally:
            self.close()

    def current(self, *, private=True):
        """FINAL NON-I/O predicate, after seal and every source/config/root read."""
        from agent import emotions
        from clover_constants import get_clover_home
        if get_clover_home() != self.home_setting:
            return False
        if not self.sealed or not self.unchanged:
            return False
        if not private:
            return True
        if self.bound is None:
            return False
        (key, entry, owner, sample, generation, ticket, host_ref, host,
         state, host_key, original_key, reader, store, input_generation,
         epoch, stop_generation, gateway_current, agent_binding) = self.bound
        # Registry absence is revocation, never permission to use cached affect.
        return (emotions._owners.get(key) is entry and entry is not None
                and entry[0] is owner and emotions._owner_hosts.get(owner) is host_ref
                and host_ref() is host and owner.enabled is True
                and owner._latest is sample and owner._generation == generation
                and owner._ticket is ticket and host.state is state and state.affect is owner
                and host.key == host_key and owner.key == host_key
                and host.original_key == original_key and host.reader is reader and host.store is store
                and host.input_generation == input_generation and host.epoch == epoch
                and host.stop_generation == stop_generation and host.gateway_current is gateway_current
                and self._agent_binding(host.agent) == agent_binding
                and self.gateway_valid
                and emotions.clock() < entry[1]
                and (sample is None or 0 <= (emotions.clock() - sample.observed_at).total_seconds() < 120))
