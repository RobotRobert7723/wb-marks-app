# Seller WB Labels API

Production base URL: `https://marksapp.sesrv.ru/api/v1`.

Current contract version: `v1.1`.

Спецификация API web-сервиса для печати этикеток из интерфейса `seller.wildberries.ru/new-goods/all-goods`.

Расширение Chrome встраивает кнопку печати в строку товара WB, получает из страницы:

- `wbStoreId` - ID магазина WB из интерфейса продавца. Пример: `4006282`.
- `storeName` - название магазина/юрлица из шапки. Пример: `ОсОО "САДИЯН"`.
- `nmId` - артикул WB. Пример: `336603350`.
- `vendorCode` - артикул продавца. Пример: `Adi_black_line_01`.
- `sizes` - размеры из internal API `GetCardInfo`, только поле `techSize`.

`wbStoreId` является основным идентификатором магазина во всех обращениях к web-сервису.

Настройки WB API и Текшер привязаны к магазину (`wbStoreId`), а не к отдельному логину. К одному магазину могут быть привязаны несколько пользователей WB Marks App.
В ссылках вида `/product-cards/{nmId}#{wbStoreId}` фрагмент после `#` доступен браузеру и плагину, но не отправляется серверу; во всех API-запросах `wbStoreId` передается отдельным полем JSON.

## Общие правила

- Формат данных: JSON.
- Кодировка: UTF-8.
- Авторизация: `Authorization: Bearer <plugin-token>`.
- WB cookies, `authorizev3`, `wb-seller-lk` и другие WB session tokens в web-сервис не передаются.
- Web-сервис должен определять магазин и его настройки по `wbStoreId`.
- Все запросы от расширения должны содержать `wbStoreId`.
- Количество этикеток `quantity` передается только для размеров, где пользователь указал значение больше `0`.

## Основные сущности

### Store identity

```json
{
  "wbStoreId": "4006282",
  "storeName": "ОсОО \"САДИЯН\""
}
```

`wbStoreId` - это ID магазина из интерфейса WB, например из блока:

```text
ОсОО "САДИЯН"
SADIYAN
ИНН 02106201810241 • ID 4006282
```

Именно `4006282` используется во всех API-запросах.

### Product context

```json
{
  "wbStoreId": "4006282",
  "storeName": "ОсОО \"САДИЯН\"",
  "nmId": "336603350",
  "vendorCode": "Adi_black_line_01",
  "sizes": ["36", "38", "40", "42", "44", "46"]
}
```

Размеры берутся из `GetCardInfo.data.sizes[].techSize`.

`wbSize` не используется.

## API endpoints

### 1. Проверка готовности к печати

```http
POST /api/v1/labels/readiness
Content-Type: application/json
Authorization: Bearer <plugin-token>
```

Расширение вызывает этот endpoint при нажатии на иконку этикетки перед открытием формы печати.

Задача endpoint:

- Проверить, что `wbStoreId` известен web-сервису.
- Проверить, что для `nmId` и размеров есть мэппинг.
- Проверить доступность Текшер.
- Вернуть, можно ли показывать форму печати как готовую.

#### Request

```json
{
  "wbStoreId": "4006282",
  "storeName": "ОсОО \"САДИЯН\"",
  "nmId": "336603350",
  "vendorCode": "Adi_black_line_01",
  "sizes": ["36", "38", "40", "42", "44", "46"]
}
```

#### Response: ready

HTTP `200`

```json
{
  "status": "ready",
  "message": "Сервис готов к печати"
}
```

#### Response: setup_required

HTTP `200`

```json
{
  "status": "setup_required",
  "message": "Не найден мэппинг или Текшер недоступен. Выполните настройки",
  "settingsUrl": "https://service.example.com/product-cards/336603350#4006282"
}
```

`settingsUrl` опционален. Если он есть, расширение может открыть его по кнопке `Настройки`.

#### Response: error

HTTP `400`, `401`, `500`

```json
{
  "status": "error",
  "message": "Описание ошибки"
}
```

### 2. Создание задания печати

```http
POST /api/v1/labels/print-jobs
Content-Type: application/json
Authorization: Bearer <plugin-token>
```

Расширение вызывает endpoint после нажатия кнопки `Печать`.

Задача endpoint:

- Создать задание печати.
- Запустить процесс: эмиссия, нанесение, трансгран, подготовка PDF.
- Вернуть `jobId`.

#### Request

```json
{
  "requestId": "wb-labels-4006282-336603350-1782245263000",
  "wbStoreId": "4006282",
  "storeName": "ОсОО \"САДИЯН\"",
  "nmId": "336603350",
  "vendorCode": "Adi_black_line_01",
  "items": [
    { "size": "36", "quantity": 2 },
    { "size": "38", "quantity": 1 }
  ]
}
```

#### Request fields

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `requestId` | string | yes | Idempotency key. Повторный запрос с тем же `requestId` не должен выпускать коды повторно. |
| `wbStoreId` | string | yes | ID магазина WB. Основной ключ магазина. Пример: `4006282`. |
| `storeName` | string | no | Название магазина для диагностики и логов. |
| `nmId` | string | yes | Артикул WB. |
| `vendorCode` | string | no | Артикул продавца WB. |
| `items` | array | yes | Список размеров и количеств. |
| `items[].size` | string | yes | Размер WB из `techSize`. |
| `items[].quantity` | integer | yes | Количество этикеток. Должно быть больше `0`. |

#### Response

HTTP `202`

```json
{
  "jobId": "job_123",
  "status": "queued",
  "rows": [
    { "size": "36", "quantity": 2, "status": "emission" },
    { "size": "38", "quantity": 1, "status": "emission" }
  ]
}
```

### 3. Получение статуса задания

```http
GET /api/v1/labels/print-jobs/{jobId}
Authorization: Bearer <plugin-token>
```

Расширение опрашивает endpoint после создания задания.

Рекомендуемый интервал опроса: 2-5 секунд.

Правила polling для UI:

- UI не должен останавливать опрос при первой строке со статусом `error`, если в задании есть другие строки со статусом `emission`, `applying` или `transgran`.
- UI обрабатывает статус каждой строки независимо от остальных строк.
- Опрос можно остановить только когда все строки находятся в терминальных статусах `ready` или `error`, а статус задания равен `done`, `partial_failed` или `error`.
- Поле `readyToPrintCount` показывает количество этикеток, готовых к печати по строке. Для строки `ready` оно должно быть равно заказанному `quantity`; после формирования PDF это количество считается распечатанным в WB Marks App.

#### Response: processing

HTTP `200`

```json
{
  "jobId": "job_123",
  "status": "processing",
  "rows": [
    { "size": "36", "quantity": 2, "status": "applying", "readyToPrintCount": 0 },
    { "size": "38", "quantity": 1, "status": "transgran", "readyToPrintCount": 0 }
  ]
}
```

#### Response: done

HTTP `200`

```json
{
  "jobId": "job_123",
  "status": "done",
  "pdfUrl": "https://service.example.com/api/v1/labels/files/file_123.pdf",
  "rows": [
    {
      "size": "36",
      "quantity": 2,
      "status": "ready",
      "readyToPrintCount": 2,
      "pdfUrl": "https://service.example.com/api/v1/labels/files/file_123.pdf"
    },
    {
      "size": "38",
      "quantity": 1,
      "status": "ready",
      "readyToPrintCount": 1,
      "pdfUrl": "https://service.example.com/api/v1/labels/files/file_123.pdf"
    }
  ]
}
```

PDF может быть один общий на все размеры. В этом случае `pdfUrl` на уровне задания и в строках может быть одинаковым.

#### Response: row error

HTTP `200`

```json
{
  "jobId": "job_123",
  "status": "partial_failed",
  "pdfUrl": "https://service.example.com/api/v1/labels/files/file_123.pdf",
  "rows": [
    {
      "size": "36",
      "quantity": 2,
      "status": "error",
      "readyToPrintCount": 0,
      "errorMessage": "Не удалось выпустить коды маркировки"
    },
    {
      "size": "38",
      "quantity": 1,
      "status": "ready",
      "readyToPrintCount": 1,
      "pdfUrl": "https://service.example.com/api/v1/labels/files/file_123.pdf"
    }
  ]
}
```

`partial_failed` означает, что часть строк завершилась успешно и PDF готов для успешных строк, но одна или несколько строк завершились ошибкой. UI должен показать PDF для строк `ready` и оставить возможность повторной попытки для строк `error`.

### 4. Повторная попытка по ошибочным строкам

```http
POST /api/v1/labels/print-jobs/{jobId}/retry
Content-Type: application/json
Authorization: Bearer <plugin-token>
```

Endpoint нужен UI плагина для восстановления строк, завершившихся ошибкой.

#### Request

```json
{
  "sizes": ["42"]
}
```

#### Request fields

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `sizes` | array | yes | Список размеров, по которым нужно повторить процесс. Повторять можно только строки задания со статусом `error`. |

#### Response

HTTP `200`

Формат ответа такой же, как у `GET /api/v1/labels/print-jobs/{jobId}`.

```json
{
  "jobId": "job_123",
  "status": "processing",
  "rows": [
    { "size": "36", "quantity": 2, "status": "ready", "readyToPrintCount": 2, "pdfUrl": "https://service.example.com/api/v1/labels/files/file_123.pdf" },
    { "size": "42", "quantity": 1, "status": "applying", "readyToPrintCount": 0 }
  ]
}
```

Правила retry:

- Повторная попытка запускается только для строк `error`, указанных в `sizes`.
- Если эмиссия уже была создана, повторная попытка не должна создавать новый заказ на эмиссию для той же строки.
- Процесс должен продолжаться с последней незавершенной операции строки: эмиссия, нанесение, трансгран или PDF.
- После успешной повторной попытки строка становится `ready`; если все строки успешны, задание становится `done`, иначе `partial_failed`.
- Если строку восстановили вручную в WB Marks App, UI плагина узнает это через следующий `GET /print-jobs/{jobId}`.

### 5. Скачивание PDF

```http
GET /api/v1/labels/files/{fileId}.pdf
Authorization: Bearer <plugin-token>
```

Возвращает PDF с этикетками.

Требования:

- PDF должен быть доступен только для заданий, созданных через этот API.
- Доступ должен проверяться по `plugin-token` и связке задания с `wbStoreId`.
- URL из `pdfUrl` должен быть готовым к скачиванию из расширения.

## Статусы задания

| Status | Meaning |
| --- | --- |
| `queued` | Задание создано и ожидает выполнения. |
| `processing` | Задание выполняется. |
| `done` | Все строки успешно завершены, PDF готов. |
| `partial_failed` | Все активные операции завершены, PDF готов для успешных строк, но одна или несколько строк завершились ошибкой. |
| `error` | Задание завершилось ошибкой, PDF не сформирован. |

## Статусы строк

| Status | Meaning |
| --- | --- |
| `emission` | Выполняется эмиссия кодов маркировки. |
| `applying` | Выполняется нанесение. |
| `transgran` | Выполняется трансгран. |
| `ready` | PDF для строки готов. |
| `error` | Ошибка по строке. Описание в `errorMessage`. |

## Ошибки

### Unauthorized

HTTP `401`

```json
{
  "status": "error",
  "message": "Unauthorized"
}
```

### Validation error

HTTP `422`

```json
{
  "status": "error",
  "message": "Не найден мэппинг для размеров: 36, 38",
  "settingsUrl": "https://service.example.com/product-cards/336603350#4006282"
}
```

### Conflict by requestId

HTTP `409`

```json
{
  "status": "error",
  "message": "requestId уже использован с другими параметрами"
}
```

Если `requestId` совпал и параметры совпадают, нужно вернуть существующий `jobId`, а не `409`.

## Требования к idempotency

`POST /api/v1/labels/print-jobs` должен быть идемпотентным по `requestId`.

Правила:

1. Если `requestId` новый, создать новое задание.
2. Если `requestId` уже есть и request body совпадает, вернуть существующее задание.
3. Если `requestId` уже есть, но request body отличается, вернуть `409`.

## Что расширение не передает

Расширение не передает в web-сервис:

- WB cookies.
- `authorizev3`.
- `wb-seller-lk`.
- Другие WB session tokens.
- Raw response internal API WB.

Web-сервис получает только бизнес-данные: `wbStoreId`, `storeName`, `nmId`, `vendorCode`, размеры и количества.

## Минимальный контракт для первой интеграции

Для первого рабочего релиза достаточно реализовать:

1. `POST /api/v1/labels/readiness`
2. `POST /api/v1/labels/print-jobs`
3. `GET /api/v1/labels/print-jobs/{jobId}`
4. `POST /api/v1/labels/print-jobs/{jobId}/retry`
5. `GET /api/v1/labels/files/{fileId}.pdf`

Обязательное поле магазина во всех request body: `wbStoreId`.
