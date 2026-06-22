# Teksher API Endpoints

Source: frontend bundle analysis from [teksher_index.js](/C:/Users/1/Documents/chrome/teksher_index.js) plus live checks against the current API.

## Dictionary endpoints

- `GET /facade/api/v1/product_groups`
  - product groups
- `GET /facade/api/v1/tnveds`
  - TN VED dictionary
- `GET /facade/api/v1/countries`
  - countries dictionary
- `GET /facade/api/v1/products/attribute_templates`
  - attribute templates and attribute-level dictionaries for a product subgroup
  - requires `subgroupId`

## Product endpoints

- `GET /facade/api/v1/products`
- `GET /facade/api/v1/products/{id}`
- `GET /facade/api/v1/products/{id}/attributes`
- `POST /facade/api/v1/products/create`
- `PUT /facade/api/v1/products/{id}`
- `PATCH /facade/api/v1/products/{id}`
- `DELETE /facade/api/v1/products/{id}`
- `POST /facade/api/v1/products/{id}/approve`
- `POST /facade/api/v1/products/{id}/reject`
- `POST /facade/api/v1/products/xlsx/create`
- `POST /facade/api/v1/products/link/{id}`

## Product creation notes

Verified live on the `mmarket` Teksher account.

### Draft creation

- `POST /facade/api/v1/products/create` creates a product card in `DRAFT`
- publishing/registration is a separate step:
  - `POST /facade/api/v1/products/{id}/approve`

### Important DTO difference

`GET /products/{id}` and `POST /products/create` do not use the same DTO.

Some fields that come back as nested objects in `GET` must be sent as scalar ids in `create`.

### Minimal accepted top-level payload shape

This payload shape passed structural validation. When sent with an already existing GTIN, the API returned `409 CONFLICT`, which confirms the body format is correct.

```json
{
  "gtin": "04700063257637",
  "fullName": "Kostyum sportivnyi 70 chernyi poliester",
  "manufacturerFullName": "Individualnyi predprinimatel Kiiyizbaev K F",
  "manufacturerInn": "21202196400195",
  "gcp": "470006325",
  "gln": "4700063250003",
  "manufacturedCountryId": 242,
  "tnved": 999,
  "trademark": "NBF",
  "isImport": false
}
```

### Required top-level fields observed from validation

- `gtin`
- `fullName`
- `manufacturerFullName`
- `manufacturerInn`
- `gcp`
- `gln`
- `manufacturedCountryId`
- `tnved`

### Field mapping from GET -> CREATE

- `gtin` -> `gtin`
- `fullName` -> `fullName`
- `trademark` -> `trademark`
- `manufacturerFullName` -> `manufacturerFullName`
- `manufacturerInn` -> `manufacturerInn`
- `gcp` -> `gcp`
- `gln` -> `gln`
- `isImport` -> `isImport`
- `tnved.id` -> `tnved`
- `manufacturedCountry.id` -> `manufacturedCountryId`

### Country field trap

These variants did not work:

- `manufacturedCountry`
- `manufacturedCountryCode`
- `country`
- `countryCode`

This variant worked:

- `manufacturedCountryId`

### Attributes payload

Frontend code strips UI-only fields before sending `attributes`.

It keeps business fields like:

- `attributeTypeCode`
- `value`
- `unitCode`

It removes UI fields like:

- `isRequired`
- `unitCodes`
- `options`
- `disabled`
- `multiplication`
- `name`

Frontend send helper found in bundle:

```text
hu = t => ({
  ...t,
  attributes: Array.isArray(t.attributes)
    ? t.attributes
        .filter(e => !!e.value?.trim() || !!e.unitCode?.trim())
        .map(({isRequired, unitCodes, options, disabled, multiplication, name, ...o}) => o)
    : []
})
```

### Example live product detail

Live `GET /facade/api/v1/products/439895713` returned a draft card with:

- `status = DRAFT`
- `tnved.id = 999`
- `manufacturedCountry.id = 242`
- attributes such as:
  - `12` `Vid tovara`
  - `35` `Razmer odezhdy / izdeliya`
  - `36` `Tsvet`
  - `2483` `Sostav`
  - `14013` `Tselevoi pol`
  - `13914` `Model / artikul proizvoditelya`
  - `13836` `Nomer reglamenta/standarta`

## Template and attachment endpoints

- `GET /facade/api/v1/attachments/product_templates`
- `GET /facade/api/v1/attachments/product_templates/download/{fileId}`

## Operations

- `GET /facade/api/v1/operations/filter`
- `GET /facade/api/v1/operations/{id}`
- `GET /facade/api/v1/operations/{id}/ready`
- `POST /facade/order/api/v1/operations/utilisation`
- `POST /facade/order/api/v1/operations/multi`
- `POST /facade/transgran/api/v1/operations/create`
- `POST /facade/transgran/api/v1/operations/cancel`

## Marking files

- `POST /facade/order/api/v1/files/marking_code`
- `POST /facade/order/api/v1/files/serial_number`
- `POST /facade/transgran/api/v1/files/marking_code`

## Marking code endpoints

- `GET /facade/api/v1/marking_codes`
- `GET /facade/api/v1/marking_codes/filter`
- `GET /facade/api/v1/marking_codes/level`
- `GET /facade/api/v1/marking_codes/csv`
- `GET /facade/api/v1/marking_codes/type_info`
- `GET /facade/api/v1/marking_codes/{id}/history`
- `GET /facade/api/v1/marking_codes/pdf/{printForm}`

## Participants and requisites

- `GET /facade/api/v1/participants`
- `GET /facade/api/v1/participants/{id}`
- `PUT /facade/api/v1/participants/{id}`
- `GET /facade/api/v1/participants/{id}/identifiers`
- `GET /facade/api/v1/participants/gns/list`
- `GET /facade/api/v1/participants/manufacturer_info`
- `GET /facade/api/v1/participants/billing/balance`
- `GET /facade/api/v1/participants/billing/transactions`
- `POST /facade/api/v1/participants/xlsx`
- `GET /facade/api/v1/qrcode`

## Notifications

- `GET /facade/api/v1/notifications/posts`
- `POST /facade/api/v1/notifications/posts/filter`
- `GET /facade/api/v1/notifications/posts/{id}`
- `GET /facade/api/v1/notifications/posts/participants/pages`
- `GET /facade/api/v1/notifications/posts/participants/unread`
- `GET /facade/api/v1/notifications/posts/participants/{id}`
- `POST /facade/api/v1/notifications/posts/publish/{id}`
- `POST /facade/api/v1/notifications/read/{postId}`
- `GET /facade/api/v1/notifications/files`
- `GET /facade/api/v1/notifications/files/{id}`

## Users

- `GET /facade/api/v1/users/filter`
- `POST /facade/api/v1/users/create`
- `GET /facade/api/v1/users/getCurrentUser`
- `PUT /facade/api/v1/users/{id}`
- `POST /facade/api/v1/users/change-password`
- `POST /facade/api/v1/users/check-password`
- `POST /facade/api/v1/users/forgot-password`
- `POST /facade/api/v1/users/reset-password`
- `GET /facade/api/v1/users/tokenCreationDate`
- `POST /facade/api/v1/users/activate?username=login`
- `POST /facade/api/v1/users/deactivate?username=login`

## Which endpoints map to our dictionaries

- `Kod TN VED` -> `/facade/api/v1/tnveds`
- `Strana proizvodstva` -> `/facade/api/v1/countries`
- `Tselevoi pol` -> `/facade/api/v1/products/attribute_templates`
- `Vid tovara` -> `/facade/api/v1/products/attribute_templates`
- `Tsvet` -> `/facade/api/v1/products/attribute_templates`
- `Razmer odezhdy / izdeliya // Tip` -> `/facade/api/v1/products/attribute_templates`
- `Model / artikul proizvoditelya // Tip` -> `/facade/api/v1/products/attribute_templates`
- `Nomer reglamenta / standarta` -> `/facade/api/v1/products/attribute_templates`

## Note

Separate endpoints like `colors`, `genders`, `product_types`, or `regulations` were not found in the frontend bundle.

Current working assumption:

- these lists come from `attribute_templates`
- the exact values depend on `subgroupId`
