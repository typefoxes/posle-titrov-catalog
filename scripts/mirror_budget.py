# Created by d.kotina on 25.11.2026
"""Консервативный месячный лимит Firebase чтений публичного зеркала; потеря job не возвращает резерв."""

import copy
import math
import uuid

from scripts.wikimedia.common import require

# MARK: - Fixed Zero-cost Policy

MONTHLY_MIRROR_STOP_BYTES = 3_000_000_000
MAXIMUM_SOURCE_BYTES = 64 * 1024**2
MAXIMUM_SOURCE_REQUESTS = 5000
CONTROL_ALLOWANCE_BYTES = 64 * 1024


def conservative_bytes(payload, requests):
    """Учитывает payload,1KiB/request, control reserve и25%запаса; не выдаёт оценку за Firebase billing."""
    require(type(payload) is int and payload >= 0 and type(requests) is int and requests >= 0,
            'invalid_mirror_transfer_counters')
    return math.ceil((payload + requests * 1024 + CONTROL_ALLOWANCE_BYTES) * 1.25)


def validate_ledger(value, month):
    """Отклоняет повреждённый учёт, отрицательные суммы и чужой месяц вместо обнуления лимита."""
    require(isinstance(value, dict) and set(value) == {'schemaVersion', 'month', 'chargedBytes', 'reservations'}
            and type(value['schemaVersion']) is int and value['schemaVersion'] == 1 and value['month'] == month
            and type(value['chargedBytes']) is int and 0 <= value['chargedBytes'] <= MONTHLY_MIRROR_STOP_BYTES
            and isinstance(value['reservations'], dict) and len(value['reservations']) <= 8
            and all(isinstance(key, str) and len(key) == 32 and all(character in '0123456789abcdef' for character in key)
                    and type(amount) is int and amount > 0 for key, amount in value['reservations'].items())
            and sum(value['reservations'].values()) <= value['chargedBytes'], 'invalid_mirror_monthly_ledger')


class MirrorReservation:
    """До первого Firebase GET фиксирует worst-case в GitHub main с blob-SHA защитой от конфликта."""

    def __init__(self, github):
        """Создаёт только идентификатор run; token и ledger не сохраняются в кэш."""
        self.github = github
        self.identifier = uuid.uuid4().hex
        self.month = None
        self.allowance = conservative_bytes(MAXIMUM_SOURCE_BYTES, MAXIMUM_SOURCE_REQUESTS)

    def reserve(self):
        """Берёт месяц из GitHub server Date; повреждённый непустой ledger не заменяет пустым."""
        _latest, server_time = self.github.request('GET', '/releases/latest')
        month = server_time.strftime('%Y-%m')
        ledger, blob_sha, confirmed_time = self.github.ledger(month)
        require(confirmed_time.strftime('%Y-%m') == month, 'mirror_month_changed_before_reserve')
        if ledger is None:
            ledger = {'schemaVersion': 1, 'month': month, 'chargedBytes': 0, 'reservations': {}}
        validate_ledger(ledger, month)
        updated = copy.deepcopy(ledger)
        require(len(updated['reservations']) < 8 and updated['chargedBytes'] + self.allowance <= MONTHLY_MIRROR_STOP_BYTES,
                'monthly_mirror_transfer_stop')
        updated['chargedBytes'] += self.allowance
        updated['reservations'][self.identifier] = self.allowance
        self.github.save_ledger(month, updated, blob_sha)
        # readback защищает от неопределённого ответа commit; без него Firebase ещё не читается.
        actual, _blob_sha, _time = self.github.ledger(month)
        require(actual == updated, 'mirror_transfer_reservation_readback_mismatch')
        self.month = month
        return {'month': month, 'reservedBytes': self.allowance, 'monthlyMirrorStopBytes': MONTHLY_MIRROR_STOP_BYTES}

    def settle(self, payload, requests):
        """Уменьшает лишь собственный резерв после завершённого run; повтор не возвращает деньги/квоту второй раз."""
        require(self.month is not None, 'mirror_transfer_not_reserved')
        measured = conservative_bytes(payload, requests)
        require(measured <= self.allowance, 'mirror_transfer_allowance_exceeded')
        ledger, blob_sha, _time = self.github.ledger(self.month)
        validate_ledger(ledger, self.month)
        if self.identifier not in ledger['reservations']:
            return {'month': self.month, 'chargedBytes': ledger['chargedBytes'], 'alreadySettled': True}
        require(ledger['reservations'][self.identifier] == self.allowance, 'mirror_transfer_reservation_changed')
        updated = copy.deepcopy(ledger)
        updated['reservations'].pop(self.identifier)
        updated['chargedBytes'] -= self.allowance - measured
        self.github.save_ledger(self.month, updated, blob_sha)
        actual, _blob_sha, _time = self.github.ledger(self.month)
        require(actual == updated, 'mirror_transfer_settlement_readback_mismatch')
        return {'month': self.month, 'chargedBytes': actual['chargedBytes'], 'billingMeasured': False}
