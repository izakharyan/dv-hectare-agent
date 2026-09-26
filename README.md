# dvhectare — поиск земли под «Дальневосточный гектар»

Стартовый проект: берёт данные из **НСПД** (nspd.gov.ru — сюда переехала публичная кадастровая карта),
вычисляет свободную от участков ЕГРН территорию в заданной области, вырезает запретные зоны,
нарезает остаток на кандидатов 100×100 м и приписывает каждому территориальную зону (ПЗЗ) с её ВРИ.

> ⚠️ **pkk.rosreestr.ru больше не работает.** Старые эндпоинты `pkk.rosreestr.ru/api/features/...`
> отключены; в конце 2024 года все open-source парсеры (rosreestr2coord, pynspd) переехали на НСПД.
> Этот проект использует актуальные эндпоинты НСПД.

## Быстрый старт (Windows, PowerShell)

```powershell
cd C:\Users\zakha\Documents\cadastral\dv-hectare-agent
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[geo,agent,dev]"
copy config.example.yaml config.yaml

pytest -q                                            # офлайн-тесты на фейковом НСПД
python -m dvhectare parcel 25:28:010013:31           # участок по КН
python -m dvhectare here --lat 43.35 --lon 132.18    # что в точке (зона, ЗОУИТ, участки)
python -m dvhectare zones --center 43.35,132.18 --radius-km 1
python -m dvhectare scan  --center 43.35,132.18 --radius-km 1
python -m dvhectare scan  --bbox 132.16,43.34,132.20,43.36 --zone "^СХ"
```

Результат `scan` — папка `out/scan_ДАТА/`:
`map.html` (карта со слоями), `candidates.geojson`, `free_area.geojson`, `parcels.geojson`,
`terr_zones.geojson`, `excl_*.geojson`, `flag_*.geojson`, `scan.gpkg` (для QGIS), `summary.json`.

### Агент на естественном языке (Gemini)

```powershell
$env:GEMINI_API_KEY = "..."    # ключ из aistudio.google.com → Get API key
python -m dvhectare agent "Найди свободные гектары в сельхоз-зонах в 1 км от 43.35, 132.18"
```
По умолчанию используется `gemini-3.8-flash`. Другую модель можно задать через `--model`
или переменную `GEMINI_MODEL` (например, `gemini-3.5-flash-lite` — дешевле).

### Раздельные прокси: НСПД напрямую, Gemini через прокси

НСПД пускает только российские IP, а Gemini API из России недоступен. Поэтому у каждого
сервиса свой прокси в `config.yaml`:

```yaml
nspd:
  proxy: null                          # НСПД — напрямую с вашего IP
gemini:
  proxy: socks5://127.0.0.1:10808      # Gemini — через локальный прокси VPN-клиента
```

- Работает только если VPN-клиент умеет режим **локального прокси** (порт на 127.0.0.1:
  v2rayN, Nekoray, Clash, Outline с proxy-режимом и т.п.). Если VPN заворачивает весь трафик
  компьютера (режим TUN/«системный VPN»), НСПД тоже уйдёт за границу и получит 403 —
  в таком случае включите в VPN-клиенте раздельное туннелирование (правило `nspd.gov.ru → direct`).
- Поддерживаются `http://`, `https://` и `socks5://` (можно с логином: `socks5://user:pass@host:port`).
- Системные переменные `HTTP(S)_PROXY` клиент НСПД игнорирует намеренно, чтобы «общий» прокси
  не утянул его за границу. Gemini их подхватывает, если `gemini.proxy` не задан.

## Как это работает

```
 НСПД ──intersects(bbox)──► ЗУ ЕГРН, «свободные от прав», терзоны, ООПТ, ТОР, ЗОУИТ, лес, НП…
   │                          (bbox автоматически дробится, если НСПД отказал: «слишком большой контур»)
   ▼
 свободно = AOI − ЗУ ЕГРН − исключающие слои − ваши доп. исключения
   ▼
 сетка 100×100 м (UTM, отступ 5 м от чужих границ) ─► кандидаты
   ▼
 каждому кандидату: терзона + её ВРИ (вкладка permissionType), флаги ЗОУИТ/лес/НП/вода
   ▼
 сортировка (сначала «чистые») ─► GeoJSON / GPKG / HTML / JSON для агента
```

| Модуль | Что делает |
|---|---|
| `dvhectare/nspd/client.py` | HTTP-клиент НСПД: поиск по КН, поиск в полигоне, GetFeatureInfo в точке, вкладки карточки; паузы, ретраи, SQLite-кэш |
| `dvhectare/layers.py` | Реестр слоёв НСПД (`layer_id` для WMS, `category_id` для поиска в полигоне) |
| `dvhectare/geo.py` | Перевод 3857→4326, UTM, bbox, сетка гектаров |
| `dvhectare/analysis.py` | `Scanner.scan()` — вся логика поиска |
| `dvhectare/sources/wfs.py` | Универсальный WFS + заготовка `FgisTpSource` под ФГИС ТП |
| `dvhectare/sources/files.py` | Локальные слои (GeoJSON/KML/GPKG/SHP) |
| `dvhectare/agent/` | Инструменты и цикл function calling для Gemini API |
| `tests/fake_nspd.py` | Фейковый НСПД для офлайн-тестов |

## Эндпоинты НСПД

| Назначение | Запрос |
|---|---|
| Поиск по КН/адресу | `GET /api/geoportal/v2/search/geoportal?thematicSearchId=1&query=<КН>` |
| Объекты в полигоне | `POST /api/geoportal/v1/intersects?typeIntersect=fullObject` + `{categories:[{id}], geom:FeatureCollection}` |
| Объекты в точке | `GET /api/aeggis/v3/{layer_id}/wms?REQUEST=GetFeatureInfo&...` |
| ВРИ терзоны и др. вкладки | `GET /api/geoportal/v1/tab-values-data?tabClass=permissionType&categoryId=..&geomId=..` |

Эндпоинты неофициальные: у НСПД нет публичного API и документации, ID слоёв иногда меняются.
Если что-то сломалось — откройте nspd.gov.ru/map, DevTools → Network, и сверьте запросы.

## Подключение ФГИС ТП и других слоёв

Слой «Территориальные зоны» уже есть в НСПД и подключён по умолчанию, но наполнен не везде.
Для муниципалитетов, где его нет:

1. Найдите WFS в DevTools на карте ФГИС ТП / ГИСОГД Приморья (запросы `GetFeature`/`GetCapabilities`).
2. Пропишите в `config.yaml`:
   ```yaml
   extra_sources:
     - {name: fgistp_pzz, type: wfs, role: zones, url: "https://…/wfs", type_name: "ws:pzz_zones"}
   ```
3. Поправьте маппинг полей в `FgisTpSource.normalize_zone()`.

Если WFS нет — выгрузите слой в GeoJSON/SHP (или отрисуйте в QGIS) и подключите `type: file`.
Так же подключаются серые зоны надальнийвосток.рф (`role: exclude`), если удастся их получить.

## Ограничения, которые нужно знать

- **НСПД доступен только с российских IP.** Из-за рубежа будет 403 → нужен российский `nspd.proxy`.
- **SSL.** Сертификат НСПД выдан НУЦ Минцифры. Корневой сертификат уже лежит в проекте
  (`certs/Russian_Trusted_Root_CA.cer`) и подключён в `config.yaml` через `nspd.ca_bundle`.
  Путь считается от папки с конфигом. Если ca_bundle убрать, клиент отключит проверку SSL.
- **Невидимые участки.** Ранее учтённые участки без координат границ на карте не видны —
  «свободная» по карте земля может оказаться чьей-то.
- **Серые зоны ДВ-гектара** (резерв, недра, КМНС, решения регионов) в НСПД не публикуются.
  Итоговая проверка — только на надальнийвосток.рф при подаче заявления.
- **Нагрузка.** Держите `min_delay` ≥ 1 с и область ≤ 25 км². Ответы кэшируются на 7 дней.
- Возможно, у НСПД есть лимит на число объектов в ответе `intersects` (не документирован).
  Если в плотной застройке видите подозрительно ровное число объектов — уменьшите bbox.
  TODO: дробить bbox, если число объектов в ответе достигло лимита.

## Что дальше

- [ ] Хранилище PostGIS вместо файлов (история сканов, дифф «что появилось/исчезло»)
- [ ] Плановые пересканы + уведомления в Telegram о новых свободных кандидатах
- [ ] Учёт рельефа/дорог (OSM) при ранжировании кандидатов
- [ ] Playwright-проверка кандидата прямо на надальнийвосток.рф
