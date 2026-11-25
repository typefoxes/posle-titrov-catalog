# Created by d.kotina on 25.11.2026
"""Проверки главных границ зеркала: durable quota, anonymous reads, cache loss и server asset digests."""

import copy
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.mirror_budget import (MAXIMUM_SOURCE_BYTES, MAXIMUM_SOURCE_REQUESTS, MONTHLY_MIRROR_STOP_BYTES,
                                  MirrorReservation, conservative_bytes)
from scripts.mirror_github import MirrorGitHub
from scripts.mirror_run import (MirrorBlockStore, PublicFirebaseSource, install_public_cache,
                                mirror_once, restore_public_seed, verified_asset_file)
from scripts.wikimedia.bundle import prepare_bundle
from scripts.wikimedia.common import CatalogError, canonical_bytes, fingerprint
from scripts.wikimedia.contract import normalize_entity
from scripts.wikimedia.disk_blocks import DiskBlockStore
from scripts.wikimedia.public_delivery import AnonymousPublicAuth
from scripts.wikimedia.public_pack import PublicPackReader, export_public_pack

# MARK: - Fixtures


def configure_ledger():
    """Использует стандартный Mock хранилища и явный synthetic UTC; настоящие часы/сервер не меняются."""
    github = Mock()
    state = {'ledger': None, 'sha': None, 'writes': 0}
    server = datetime(2026, 11, 25, tzinfo=timezone.utc)
    github.request.return_value = ({'tag_name': 'synthetic'}, server)

    def read_ledger(month):
        """Возвращает независимую копию и synthetic CAS token текущего ledger."""
        return copy.deepcopy(state['ledger']), state['sha'], server

    def save_ledger(month, ledger, expected_sha):
        """Не позволяет side effect теста незаметно перезаписать чужой blob SHA."""
        if expected_sha != state['sha']:
            raise CatalogError('synthetic_CAS_conflict')
        state['ledger'] = copy.deepcopy(ledger)
        state['writes'] += 1
        state['sha'] = str(state['writes'])
        return state['sha']

    github.ledger.side_effect = read_ledger
    github.save_ledger.side_effect = save_ledger
    return github, state


def public_fixture(root, revision=100):
    """Собирает маленький настоящий public codec пакет; данные synthetic, source runtime не заявляется."""
    entity = {'id': 'Q123', 'type': 'item', 'lastrevid': revision, 'labels': {'ru': {'language': 'ru', 'value': 'Пример'}},
              'descriptions': {}, 'aliases': {}, 'sitelinks': {}, 'claims': {}}
    records = {'Q123': normalize_entity(entity, 'film', '2026-11-25T00:00:00+00:00')}
    assets = {'inputSHA256': fingerprint(records), 'completeForRequestedSample': True,
              'collections': {'synopses': {}, 'related': {}, 'references': {}}}
    bundle = prepare_bundle(records, assets)
    catalog = {'schemaVersion': 1, 'encoding': 'zlib-base64', 'media': 'no_verified_posters', 'fullScopeVerified': True,
               'sourceSnapshot': 'a' * 64, 'sourceObservedAt': '2026-11-25T00:00:00+00:00',
               'collections': bundle['manifest']['collections']}
    store = DiskBlockStore(root / 'objects')
    try:
        for digest, envelope in bundle['blocks'].items():
            store.put(digest, envelope)
        manifest = export_public_pack(catalog, store, root / 'package')
    finally:
        store.close()
    return manifest, bundle['blocks']


# MARK: - Monthly Reservation


class MirrorBudgetTests(unittest.TestCase):
    """Не позволяет потерянному job, cache miss или повтору settlement обнулить месячный stop."""

    def test_reservation_precedes_reads_and_failed_run_keeps_full_allowance(self):
        """Новый экземпляр run учитывает незакрытую reservation предыдущего, а не создаёт новый месячный бюджет."""
        github, state = configure_ledger()
        first = MirrorReservation(github)
        first.reserve()
        allowance = conservative_bytes(MAXIMUM_SOURCE_BYTES, MAXIMUM_SOURCE_REQUESTS)
        self.assertEqual(state['ledger']['chargedBytes'], allowance)
        second = MirrorReservation(github)
        second.reserve()
        self.assertEqual(state['ledger']['chargedBytes'], 2 * allowance)
        self.assertEqual(len(state['ledger']['reservations']), 2)

    def test_settlement_is_idempotent_and_never_releases_another_run(self):
        """Refund снимает только собственную разницу worst-case/actual; второй settlement не повторяет списание."""
        github, state = configure_ledger()
        first, second = MirrorReservation(github), MirrorReservation(github)
        first.reserve()
        second.reserve()
        first.settle(1024, 4)
        self.assertEqual(state['ledger']['chargedBytes'], second.allowance + conservative_bytes(1024, 4))
        self.assertEqual(set(state['ledger']['reservations']), {second.identifier})
        writes = state['writes']
        first.settle(1024, 4)
        self.assertEqual(state['writes'], writes)

    def test_limit_and_corruption_fail_before_reservation_commit(self):
        """Исчерпанный или повреждённый ledger не заменяется нулевым новым файлом."""
        for charged, expected in ((MONTHLY_MIRROR_STOP_BYTES, 'transfer_stop'), (-1, 'invalid_mirror')):
            github, state = configure_ledger()
            state['ledger'] = {'schemaVersion': 1, 'month': '2026-11', 'chargedBytes': charged, 'reservations': {}}
            with self.assertRaisesRegex(CatalogError, expected):
                MirrorReservation(github).reserve()
            self.assertEqual(state['writes'], 0)

    def test_failed_CAS_or_readback_does_not_grant_source_permission(self):
        """Неуспешный durable commit не переводит объект reservation в разрешённое состояние."""
        github, _state = configure_ledger()
        github.save_ledger.side_effect = CatalogError('synthetic_CAS_conflict')
        reservation = MirrorReservation(github)
        with self.assertRaises(CatalogError):
            reservation.reserve()
        self.assertIsNone(reservation.month)

    def test_settlement_above_reserved_worst_case_fails(self):
        """Нельзя вернуть резерв и скрыть трафик, превышающий разрешённую верхнюю границу run."""
        github, _state = configure_ledger()
        reservation = MirrorReservation(github)
        reservation.reserve()
        with self.assertRaisesRegex(CatalogError, 'allowance_exceeded'):
            reservation.settle(MAXIMUM_SOURCE_BYTES + 1024, MAXIMUM_SOURCE_REQUESTS)


# MARK: - Source HTTP Boundary


class MirrorSourceTests(unittest.TestCase):
    """Проверяет только публичный fixed Firebase namespace без настоящей сети и credentials."""

    def test_fixed_public_address_auth_and_hash(self):
        """Полный SHA проверяется; token/cookies нельзя получить из закрытого importer источника."""
        value = {'synthetic': True}
        session = Mock()
        response = Mock(status_code=200)
        response.raw.read.return_value = canonical_bytes(value)
        session.get.return_value = response
        source = PublicFirebaseSource(session)
        self.assertEqual(source.read('blocks', fingerprint(value)), value)
        call = session.get.call_args
        self.assertIn('/catalog/wikimedia/v1/blocks/' + fingerprint(value), call.args[0])
        self.assertIsInstance(call.kwargs['auth'], AnonymousPublicAuth)
        self.assertTrue(call.kwargs['verify'])
        self.assertFalse(call.kwargs['allow_redirects'])

    def test_closed_or_non_hash_paths_never_reach_network(self):
        """Control, checkpoint, root и произвольный digest недоступны даже через общий helper."""
        session = Mock()
        source = PublicFirebaseSource(session)
        for collection, digest in (('stateBlocks', 'a' * 64), ('blocks', '../control'), ('checkpoints', 'a' * 64)):
            with self.assertRaises(CatalogError):
                source.read(collection, digest)
        session.get.assert_not_called()

    def test_unknown_response_bytes_and_stop_are_conservative(self):
        """Обрыв не обнуляет неизвестные partial bytes; следующий GET останавливается до превышения64MiB."""
        session = Mock()
        session.get.side_effect = OSError('synthetic-network-error')
        source = PublicFirebaseSource(session)
        with self.assertRaisesRegex(CatalogError, 'network_or_json_error'):
            source.read('blocks', 'a' * 64)
        self.assertEqual(source.received, 257 * 1024 + 1)
        source.received = MAXIMUM_SOURCE_BYTES
        with self.assertRaisesRegex(CatalogError, 'budget_exhausted'):
            source.read('blocks', 'a' * 64)
        self.assertEqual(session.get.call_count, 1)

    def test_source_hash_mismatch_is_not_cached_or_accepted(self):
        """Данные другого QID/hash не превращаются в успешный сетевой hit."""
        session = Mock()
        session.get.return_value = Mock(status_code=200)
        session.get.return_value.raw.read.return_value = b'{}'
        with self.assertRaisesRegex(CatalogError, 'hash_mismatch'):
            PublicFirebaseSource(session).read('blocks', 'a' * 64)


# MARK: - Public Cache and Publication Boundary


class MirrorCacheTests(unittest.TestCase):
    """Cache остаётся disposable: его потеря/порча не требует холодного чтения всего Firebase каталога."""

    def setUp(self):
        """Создаёт отдельный временный public fixture и каталог cache."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest, self.blocks = public_fixture(self.root)

    def read_range(self, name, offset, length, total):
        """Читает точный fixture range вместо сети и сохраняет bounded размер каждой порции."""
        self.assertLessEqual(length, 1024**2)
        path = self.root / 'package' / name
        self.assertEqual(path.stat().st_size, total)
        with path.open('rb') as stream:
            stream.seek(offset)
            return stream.read(length)

    def test_cold_seed_restores_from_public_assets_and_reuses_verified_cache(self):
        """Первый проход скачивает только GitHub seed; повтор после полногоSHA делает0сетевыхчтений."""
        transport = Mock()
        transport.read_range.side_effect = self.read_range
        cache = self.root / 'cache'
        reader = restore_public_seed(transport, self.manifest, cache)
        self.assertEqual(reader.get(next(iter(self.blocks))), next(iter(self.blocks.values())))
        calls = transport.read_range.call_count
        restore_public_seed(transport, self.manifest, cache)
        self.assertEqual(transport.read_range.call_count, calls)
        (cache / 'catalog.pack').write_bytes(b'corrupted')
        restore_public_seed(transport, self.manifest, cache)
        self.assertGreater(transport.read_range.call_count, calls)

    def test_new_inventory_contains_only_visited_public_objects(self):
        """Неиспользованный объект старого пакета не попадает в новый export inventory."""
        source = Mock()
        store = MirrorBlockStore(self.root / 'next-objects', self.blocks, source)
        try:
            digest = next(iter(self.blocks))
            self.assertEqual(store.get(digest), self.blocks[digest])
            self.assertEqual(store.inventory()['uniqueBlocks'], 1)
            source.read.assert_not_called()
        finally:
            store.close()

    def test_new_missing_sha_is_fetched_once_and_hash_verified(self):
        """Fallback существует только для адресного обновления mirror, а не для массового iOS чтения."""
        source = Mock()
        digest, value = next(iter(self.blocks.items()))
        source.read.return_value = value
        store = MirrorBlockStore(self.root / 'next-objects', {}, source)
        try:
            self.assertEqual(store.get(digest), value)
            self.assertEqual(store.get(digest), value)
            source.read.assert_called_once_with('blocks', digest)
            self.assertEqual(store.new_source_blocks, 1)
        finally:
            store.close()

    def test_cache_install_checks_full_assets_and_is_readable(self):
        """После публикации новый cache берётся из локальной проверенной сборки без повторного networkdownload."""
        cache = self.root / 'cache'
        install_public_cache(self.root / 'package', self.manifest, cache)
        self.assertTrue(verified_asset_file(cache / 'catalog.pack', self.manifest['pack']))
        reader = PublicPackReader(self.manifest, self.read_range)
        self.assertEqual(reader.get(next(iter(self.blocks))), next(iter(self.blocks.values())))

    def test_interrupted_delta_resumes_without_fetching_confirmed_prefix_again(self):
        """После bounded остановки следующий job читает уже полученный SHA с диска и продолжает оставшиеся адреса."""
        first, second = list(self.blocks.items())[:2]
        pending = self.root / 'cache/pending'
        source = Mock()
        source.read.side_effect = [first[1], CatalogError('mirror_source_budget_exhausted')]
        store = MirrorBlockStore(self.root / 'attempt-first', {}, source, pending)
        try:
            self.assertEqual(store.get(first[0]), first[1])
            with self.assertRaisesRegex(CatalogError, 'source_budget_exhausted'):
                store.get(second[0])
        finally:
            store.close()
        resumed_source = Mock()
        resumed_source.read.return_value = second[1]
        resumed = MirrorBlockStore(self.root / 'attempt-second', {}, resumed_source, pending)
        try:
            self.assertEqual(resumed.get(first[0]), first[1])
            self.assertEqual(resumed.get(second[0]), second[1])
            resumed_source.read.assert_called_once_with('blocks', second[0])
            self.assertEqual(resumed.reused_pending_blocks, 1)
            self.assertEqual(resumed.new_source_blocks, 1)
        finally:
            resumed.close()

    def test_corrupted_pending_body_is_quarantined_before_addressed_repair(self):
        """Имя SHA и строка inventory не позволяют принять повреждённый cache как опубликованное содержимое."""
        digest, value = next(iter(self.blocks.items()))
        pending = self.root / 'cache/pending'
        damaged = DiskBlockStore(pending)
        damaged.put(digest, value)
        damaged.path(digest).write_bytes(b'{"corrupted":true}')
        damaged.close()
        source = Mock()
        source.read.return_value = value
        store = MirrorBlockStore(self.root / 'repaired', {}, source, pending)
        try:
            self.assertEqual(store.get(digest), value)
            source.read.assert_called_once_with('blocks', digest)
            self.assertEqual(store.reused_pending_blocks, 0)
            self.assertEqual(store.pending.get(digest), value)
            self.assertEqual(len(list(pending.rglob('*.corrupt-*'))), 1)
        finally:
            store.close()

    def test_unvisited_pending_blocks_do_not_enter_new_package_inventory(self):
        """Смена закреплённого source использует только достигнутые SHA; чужой partial DAG не добавляется в export."""
        first, second = list(self.blocks.items())[:2]
        pending = DiskBlockStore(self.root / 'cache/pending')
        pending.put(first[0], first[1])
        pending.put(second[0], second[1])
        pending.close()
        (self.root / 'cache/pending/inventory.sqlite').write_bytes(b'corrupted unused index')
        source = Mock()
        store = MirrorBlockStore(self.root / 'selected-dag', {}, source, self.root / 'cache/pending')
        try:
            self.assertEqual(store.get(second[0]), second[1])
            inventory = {row[0] for row in store.connection.execute('SELECT digest FROM blocks')}
            self.assertEqual(inventory, {second[0]})
            source.read.assert_not_called()
            self.assertTrue(store.clear_completed_pending())
            self.assertFalse((self.root / 'cache/pending').exists())
        finally:
            store.close()

    def test_pending_symlink_never_reads_or_changes_its_external_target(self):
        """Disposable cache не позволяет symlink читать внешний файл; карантин переносит лишь саму ссылку."""
        digest, value = next(iter(self.blocks.items()))
        source = Mock()
        source.read.return_value = value
        store = MirrorBlockStore(self.root / 'safe', {}, source, self.root / 'cache/pending')
        external = self.root / 'outside-cache.json'
        external.write_bytes(b'{"external":"untouched synthetic fixture"}')
        target = store.pending.path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(external)
        try:
            self.assertEqual(store.get(digest), value)
            self.assertEqual(external.read_bytes(), b'{"external":"untouched synthetic fixture"}')
            self.assertFalse(target.is_symlink())
            source.read.assert_called_once_with('blocks', digest)
        finally:
            store.close()

    def test_complete_mirror_resumes_bounded_runs_and_publishes_only_after_full_dag_and_source_check(self):
        """Настоящий export проходит несколько jobs с budget4GET; partial не публикуется, а новый пакет читается целиком."""
        target, blocks = public_fixture(self.root / 'target', revision=101)
        release = {'schemaVersion': 1, 'catalog': target['catalog'], 'sourceSnapshot': target['catalog']['sourceSnapshot'],
                   'checkpoint': target['catalog']['sourceSnapshot'], 'readbackVerified': True}
        pointer = {'schemaVersion': 1, 'release': fingerprint(release), 'sourceSnapshot': release['sourceSnapshot'],
                   'generation': 2, 'publisher': {'owner': 'a' * 32, 'fence': 1}, 'activatedAt': '2026-11-25T00:00:00+00:00'}
        github, ledger = configure_ledger()
        previous_tag = 'wiki-' + fingerprint(self.manifest)
        github.latest_release.return_value = {'tag_name': previous_tag}
        state = {'published': None, 'sourceBlockReads': [], 'failedJobs': 0}

        for attempt in range(6):
            source, transport = Mock(), Mock()
            source.requests, source.received = 0, 0
            transport.tag = previous_tag
            transport.read_manifest.return_value = self.manifest

            def read(collection=None, digest=None):
                """Ограничивает каждый run до4GET с точным учётом, включая финальную проверку source."""
                if source.requests == 4:
                    raise CatalogError('mirror_source_budget_exhausted')
                source.requests += 1
                if collection is None:
                    value = pointer
                elif collection == 'releases':
                    value = release
                else:
                    state['sourceBlockReads'].append(digest)
                    value = blocks[digest]
                source.received += len(canonical_bytes(value))
                return value

            def read_range(name, offset, length, total):
                """До публикации читает прежний seed, затем независимо проверяет собранные новые assets."""
                directory = state['published'] or self.root / 'package'
                path = directory / name
                self.assertEqual(path.stat().st_size, total)
                with path.open('rb') as stream:
                    stream.seek(offset)
                    return stream.read(length)

            def publish(directory, _digest, previous, _commit, confirm_source):
                """Имитирует последнюю границу GitHub API; не открывает пакет до успешной проверки pointer."""
                self.assertEqual(previous, previous_tag)
                confirm_source()
                state['published'] = directory
                transport.read_manifest.return_value = target
                return {'published': True}

            source.read.side_effect = read
            transport.read_range.side_effect = read_range
            github.publish_package.side_effect = publish
            directory = self.root / ('bounded-run-' + str(attempt))
            directory.mkdir()
            with patch('scripts.mirror_run.PublicFirebaseSource', return_value=source), \
                    patch('scripts.mirror_run.ReleaseRangeTransport', return_value=transport), \
                    patch.dict(os.environ, {'GITHUB_SHA': 'b' * 40}):
                try:
                    result = mirror_once(github, directory, self.root / 'resumable-cache')
                except CatalogError as error:
                    self.assertEqual(str(error), 'mirror_source_budget_exhausted')
                    self.assertIsNone(state['published'])
                    state['failedJobs'] += 1
                else:
                    self.assertTrue(result['complete'])
                    self.assertTrue(result['pendingCacheCleared'])
                    break
            self.assertLessEqual(source.requests, 4)
            self.assertFalse(ledger['ledger']['reservations'])
        self.assertIsNotNone(state['published'])
        self.assertGreater(state['failedJobs'], 0)
        self.assertEqual(len(state['sourceBlockReads']), len(set(state['sourceBlockReads'])))
        reader = PublicPackReader(target, read_range)
        for digest, envelope in blocks.items():
            self.assertEqual(reader.get(digest), envelope)
        self.assertFalse(ledger['ledger']['reservations'])


class MirrorGitHubBoundaryTests(unittest.TestCase):
    """Не позволяет ephemeral token использовать за пределами собственного public mirror API."""

    def test_unknown_repo_routes_never_reach_network(self):
        """ПроизвольныйURL, Secrets и settings не входят в разрешённый API writer."""
        session = Mock()
        github = MirrorGitHub('synthetic-token-not-real', session)
        for path in ('https://example.invalid/', '/contents/Secrets/key.json', '/settings', '/actions/secrets'):
            with self.assertRaises(CatalogError):
                github.request('PUT', path, {})
        session.request.assert_not_called()

    def test_API_never_follows_token_redirect(self):
        """Неожиданный302не переносит Bearer на uploads/CDN или другойhost."""
        session = Mock()
        session.request.return_value = Mock(status_code=302)
        github = MirrorGitHub('synthetic-token-not-real', session)
        with self.assertRaises(CatalogError):
            github.request('GET', '/releases/latest')
        self.assertEqual(session.request.call_count, 1)
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])

    def test_wrong_server_asset_digest_does_not_publish_draft(self):
        """Неполный upload/другой SHA останавливает публикацию до переключенияlatest."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.joinpath('catalog.pack').write_bytes(b'abc')
            github = MirrorGitHub('synthetic-token-not-real', Mock())
            github.request = Mock(side_effect=[({'id': 123, 'draft': True, 'tag_name': 'wiki-' + 'a' * 64, 'assets': []}, None),
                ({'name': 'catalog.pack', 'state': 'uploaded', 'size': 3, 'digest': 'sha256:' + '0' * 64}, None)])
            callback = Mock()
            with self.assertRaisesRegex(CatalogError, 'digest_mismatch'):
                github.publish_package(directory, 'a' * 64, 'previous', 'b' * 40, callback)
            callback.assert_not_called()
            self.assertTrue(all(call.args[0] != 'PATCH' for call in github.request.call_args_list))

    def test_failed_empty_draft_starter_can_resume_without_touching_uploaded_asset(self):
        """После реального класса502разрешён лишь zero-byte starter; uploaded/public release остаётся защищённым."""
        tag = 'wiki-' + 'a' * 64
        starter = {'id': 456, 'name': 'catalog.pack', 'state': 'starter', 'size': 0, 'digest': None}
        github = MirrorGitHub('synthetic-token-not-real', Mock())
        github.request = Mock(side_effect=[({'draft': True, 'tag_name': tag, 'assets': [starter]}, None), (None, None),
                                           ({'draft': True, 'tag_name': tag, 'assets': []}, None)])
        github.delete_failed_draft_asset(123, 456, tag)
        self.assertEqual(github.request.call_args_list[1].args, ('DELETE', '/releases/assets/456'))
        self.assertIsNone(github.deletable_asset_id)
        for draft, state in ((False, 'starter'), (True, 'uploaded')):
            github.request = Mock(return_value=({'draft': draft, 'tag_name': tag,
                'assets': [{**starter, 'state': state}]}, None))
            with self.assertRaises(CatalogError):
                github.delete_failed_draft_asset(123, 456, tag)
            self.assertEqual(github.request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
