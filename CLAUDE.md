# Rubedo 5.0 — заметки для себя (и другого себя)

## Severity: спека ≠ код

`doc rubedo 5/rubedo5-spec.md` §7 называет три уровня уведомлений
`critical` / `important` / `routine`. Реально реализовано в
`agent/notify.py` — `critical` / `normal` / `low` (решение было принято
при первой сборке §7, спека с тех пор не переименована). Соответствие:

| Спека (§7)  | Код (`agent/notify.py`) |
|-------------|--------------------------|
| critical    | `critical`              |
| important   | `normal`                |
| routine     | `low`                   |

Уже дважды приводило к багу, когда новый код использовал буквально
`"important"` (несуществующий уровень → `should_deliver()` всегда
`False` → сообщение молча бандлится и никогда не доставляется):
`agent/crash_recovery.py` (этап 7.5) и `day/reminders.py` (этап 9.5).
При добавлении нового вызова `notify.deliver()`/`should_deliver()` —
сверяться с этой таблицей, не с §7 текстом напрямую.
