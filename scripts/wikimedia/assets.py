# Created by d.kotina on 24.11.2026
"""Возобновляемая подготовка атрибутированного текста, справочников и связей эпизодов."""

import hashlib
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

from scripts.wikimedia.common import (CatalogError, atomic_json, canonical_bytes, fingerprint,
                                     read_json, require, valid_qid, valid_wikibase_identifier)
from scripts.wikimedia.contract import (PROPERTY_FIELDS, entity_source, ordered_map,
                                       statement_projection, validate_firebase_value)
from scripts.wikimedia.enrichment import cached_action
from scripts.wikimedia.measure import fetch_entities

# MARK: - Plain Text

class PlotHTMLParser(HTMLParser):
    """Извлекает текст абзацев/списков без исполнения HTML и без подписей, сносок и навигации."""

    excluded_tags = {"script", "style", "table", "figure", "sup", "noscript", "math"}
    void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
    excluded_classes = {"mw-editsection", "reference", "references", "reflist", "navbox", "thumb", "hatnote"}

    def __init__(self):
        """Создаёт независимый стек исключённых элементов и выходной текст."""
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.fragments = []

    def handle_starttag(self, tag, attrs):
        """Отмечает исключённые поддеревья, сохраняя границы абзацев и переносов."""
        attributes = dict(attrs)
        excluded = (tag in self.excluded_tags or bool(self.excluded_classes.intersection(
            (attributes.get("class") or "").split())) or "hidden" in attributes
            or attributes.get("aria-hidden") == "true")
        blocked = excluded or any(entry[1] for entry in self.stack)
        if tag in {"p", "li", "br"} and not blocked:
            self.fragments.append("\n")
        if tag not in self.void_tags:
            self.stack.append((tag, blocked))

    def handle_startendtag(self, tag, attrs):
        """Обрабатывает самозакрытый тег без оставления ложного родителя в стеке."""
        self.handle_starttag(tag, attrs)
        if tag not in self.void_tags:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        """Закрывает совпадающий элемент, не позволяя неверной вложенности раскрыть скрытый текст."""
        positions = [position for position, entry in enumerate(self.stack) if entry[0] == tag]
        if positions:
            position = positions[-1]
            if tag in {"p", "li"} and not any(entry[1] for entry in self.stack):
                self.fragments.append("\n")
            del self.stack[position:]

    def handle_data(self, data):
        """Берёт только видимый текст внутри абзаца или элемента списка."""
        if any(entry[0] in {"p", "li"} for entry in self.stack) and not any(entry[1] for entry in self.stack):
            self.fragments.append(data)

    def plain_text(self):
        """Нормализует пробелы и пустые строки, не пересказывая и не переводя исходный сюжет."""
        paragraphs = [re.sub(r"\s+", " ", line).strip() for line in "".join(self.fragments).split("\n")]
        return "\n\n".join(line for line in paragraphs if line)


def synopsis_document(qid, title, page_id, revision, rendered_html, retrieved_at, cleaned_text=None):
    """Создаёт текстовый документ с фиксированной ревизией, лицензией и явным описанием обработки."""
    require(valid_qid(qid) and isinstance(title, str) and bool(title)
            and type(page_id) is int and page_id > 0 and type(revision) is int and revision > 0,
            "invalid_synopsis_identity")
    require(isinstance(rendered_html, str) and len(rendered_html.encode("utf-8")) <= 4 * 1024 * 1024,
            "synopsis_html_too_large")
    if cleaned_text is None:
        parser = PlotHTMLParser()
        parser.feed(rendered_html)
        parser.close()
        text = parser.plain_text()
    else:
        require(isinstance(cleaned_text, str), "invalid_cleaned_plot")
        text = cleaned_text
    require(bool(text), "empty_cleaned_plot")
    require(len(text.encode("utf-8")) <= 128 * 1024, "synopsis_text_too_large")
    source_url = "https://ru.wikipedia.org/w/index.php?oldid=" + str(revision)
    article_url = "https://ru.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="")
    result = {"schemaVersion": 1, "qid": qid, "kind": "synopsis", "language": "ru", "text": text,
        "source": {"provider": "ruwiki", "pageId": page_id, "title": title, "revision": revision,
                   "url": source_url, "articleURL": article_url, "retrievedAt": retrieved_at,
                   "renderedHTMLSHA256": hashlib.sha256(rendered_html.encode("utf-8")).hexdigest()},
        "attribution": {"credit": "Авторы статьи «" + title + "» в Википедии",
            "historyURL": "https://ru.wikipedia.org/w/index.php?title=" + quote(title, safe="") + "&action=history",
            "license": "CC-BY-SA-4.0", "licenseURL": "https://creativecommons.org/licenses/by-sa/4.0/",
            "modified": True, "changes": "Извлечён раздел «Сюжет»; удалены оформление, ссылки, сноски и служебные элементы.",
            "processingVersion": 1}}
    validate_asset(result)
    return result


# MARK: - Wikipedia Collection

def collect_synopses(client, records, directory, source_cache):
    """Продолжает по завершённым документам; ошибки страницы остаются ошибками, а не отсутствием сюжета."""
    directory, source_cache = Path(directory), Path(source_cache)
    rights = cached_action(client, source_cache, "ru.wikipedia.org",
        {"action": "query", "meta": "siteinfo", "siprop": "rightsinfo"})
    require(rights.get("query", {}).get("rightsinfo", {}).get("url") in (
        "https://creativecommons.org/licenses/by-sa/4.0/", "https://creativecommons.org/licenses/by-sa/4.0/deed.ru",
        "//creativecommons.org/licenses/by-sa/4.0/"), "wikipedia_license_requires_review")
    documents, statuses = {}, {}
    for qid, record in sorted(records.items()):
        candidate = record.get("wikipediaCandidates", {}).get("ru")
        if not candidate:
            statuses[qid] = {"state": "no_linked_page"}
            continue
        destination = directory / "synopses" / (qid + ".json")
        try:
            pages = cached_action(client, source_cache, "ru.wikipedia.org", {"action": "query",
                "titles": candidate["title"], "redirects": 1, "prop": "pageprops"}).get("query", {}).get("pages", {})
            require(len(pages) == 1, "wikipedia_page_ambiguous")
            page = next(iter(pages.values()))
            require(page.get("pageprops", {}).get("wikibase_item") == qid, "wikipedia_qid_mismatch")
            parsed = cached_action(client, source_cache, "ru.wikipedia.org", {"action": "parse", "page": page["title"],
                "prop": "tocdata|revid"}).get("parse", {})
            require(parsed.get("pageid") == page.get("pageid") and type(parsed.get("revid")) is int
                    and isinstance(parsed.get("tocdata", {}).get("sections"), list), "wikipedia_partial_sections")
            sections = [item for item in parsed["tocdata"]["sections"]
                        if re.sub(r"<[^>]+>", "", item.get("line", "")).strip().casefold() == "сюжет"]
            if not sections:
                statuses[qid] = {"state": "no_plot_section", "revision": parsed["revid"]}
                continue
            require(len(sections) == 1, "ambiguous_plot_section")
            if destination.exists():
                document = read_json(destination)
                validate_asset(document)
                require(document["qid"] == qid and document["source"]["revision"] == parsed["revid"]
                        and document["source"]["pageId"] == page["pageid"], "synopsis_resume_source_changed")
            else:
                parameters = {"action": "parse", "oldid": parsed["revid"], "section": sections[0]["index"],
                              "prop": "text|revid", "disableeditsection": 1, "disablelimitreport": 1}
                rendered = cached_action(client, directory / "wikipedia-html", "ru.wikipedia.org", parameters).get("parse", {})
                require(rendered.get("revid") == parsed["revid"] and rendered.get("pageid") == page["pageid"],
                        "wikipedia_revision_mismatch")
                cache_path = directory / "wikipedia-html" / (fingerprint(
                    {"host": "ru.wikipedia.org", "parameters": parameters}) + ".json")
                document = synopsis_document(qid, page["title"], page["pageid"], parsed["revid"],
                    rendered.get("text", {}).get("*"), datetime.fromtimestamp(cache_path.stat().st_mtime, timezone.utc).isoformat())
                atomic_json(destination, document)
            documents[qid] = document
            statuses[qid] = {"state": "available", "revision": parsed["revid"]}
        except CatalogError as error:
            statuses[qid] = {"state": "error", "error": str(error)}
        atomic_json(directory / "synopsis-status.json", statuses)
    atomic_json(directory / "synopsis-status.json", statuses)
    return documents, statuses


# MARK: - Related Entities

def related_query(identifiers, maximum_rows):
    """Ограничивает связи сезонами и эпизодами выбранных произведений, не глобальным графом."""
    require(bool(identifiers) and len(identifiers) <= 50 and all(valid_qid(qid) for qid in identifiers),
            "invalid_related_batch")
    require(type(maximum_rows) is int and 1 <= maximum_rows <= 500, "invalid_relation_limit")
    return ('SELECT DISTINCT ?show ?item ?role WHERE { hint:Query hint:optimizer "None" . VALUES ?show { '
        + " ".join("wd:" + qid for qid in sorted(identifiers)) + " } { ?parent (wdt:P179|wdt:P361)? ?show . "
        '?item (wdt:P179|wdt:P361|wdt:P4908) ?parent; wdt:P31/wdt:P279* wd:Q21191270 . BIND("episode" AS ?role) } '
        'UNION { ?item (wdt:P179|wdt:P361) ?show; wdt:P31/wdt:P279* wd:Q3464665 . BIND("season" AS ?role) } '
        "} ORDER BY STR(?show) STR(?item) ?role LIMIT " + str(maximum_rows + 1))


def related_metadata(entity, role, retrieved_at, relation_hash):
    """Готовит source metadata без неподтверждённых works; до разрешения связей это не публичный asset."""
    require(role in {"season", "episode"}, "invalid_related_role")
    result = {"schemaVersion": 1, "qid": entity.get("id"), "kind": role,
              "source": entity_source(entity, retrieved_at),
              "relationQuerySHA256": relation_hash}
    labels = {language: item["value"] for language, item in entity.get("labels", {}).items()
              if language in {"ru", "en"} and isinstance(item.get("value"), str) and item["value"]}
    if labels:
        result["titles"] = labels
    facts = {}
    for property_id in ("P31", "P179", "P361", "P4908", "P527", "P1545", "P577", "P580", "P582", "P2047", "P345"):
        statements = [statement_projection(item) for item in entity.get("claims", {}).get(property_id, [])]
        if statements:
            facts[PROPERTY_FIELDS[property_id]] = ordered_map(statements, "s")
    if facts:
        result["facts"] = facts
    return result


def normalize_related(entity, role, works, retrieved_at, relation_hash):
    """Сохраняет QID, исходные qualifiers и точность дат; локальные номера прогресса не выводятся."""
    require(bool(works) and all(valid_qid(qid) for qid in works), "invalid_related_identity")
    result = related_metadata(entity, role, retrieved_at, relation_hash)
    result["works"] = {qid: True for qid in sorted(works)}
    validate_asset(result)
    return result


def collect_related(client, records, directory, maximum_rows=500):
    """Кэширует bounded WDQS-выдачу и каждую нормализованную сущность для повторного запуска."""
    directory = Path(directory)
    identifiers = sorted(qid for qid, record in records.items() if record["kind"] != "film")
    if not identifiers:
        return {}, {"state": "no_series", "completeForPattern": True}
    query = related_query(identifiers, maximum_rows)
    query_path = directory / ("relations-" + fingerprint(query) + ".json")
    if not query_path.exists():
        atomic_json(query_path, {"query": query, "response": client.sparql(query),
                                "retrievedAt": datetime.now(timezone.utc).isoformat()})
    snapshot = read_json(query_path)
    require(snapshot["query"] == query, "relation_cache_mismatch")
    rows = snapshot["response"].get("results", {}).get("bindings")
    require(isinstance(rows, list) and len(rows) <= maximum_rows, "relation_page_limit_reached")
    relations = {}
    for row in rows:
        show = row.get("show", {}).get("value", "").removeprefix("http://www.wikidata.org/entity/")
        qid = row.get("item", {}).get("value", "").removeprefix("http://www.wikidata.org/entity/")
        role = row.get("role", {}).get("value")
        require(show in identifiers and valid_qid(qid) and role in {"season", "episode"}, "invalid_relation_row")
        relation = relations.setdefault(qid, {"role": role, "works": set()})
        require(relation["role"] == role, "ambiguous_related_role")
        relation["works"].add(show)
    documents = {}
    pending = []
    for qid in sorted(relations):
        path = directory / "related" / (qid + ".json")
        if path.exists():
            document = read_json(path)
            validate_asset(document)
            require(document["qid"] == qid and document["kind"] == relations[qid]["role"]
                    and set(document["works"]) == relations[qid]["works"]
                    and document["relationQuerySHA256"] == fingerprint(query), "related_resume_source_changed")
            documents[qid] = document
        else:
            pending.append(qid)
    for offset in range(0, len(pending), 25):
        entities = fetch_entities(client, pending[offset:offset + 25])
        for qid, entity in entities.items():
            document = normalize_related(entity, relations[qid]["role"], relations[qid]["works"],
                                         snapshot["retrievedAt"], fingerprint(query))
            atomic_json(directory / "related" / (qid + ".json"), document)
            documents[qid] = document
    return documents, {"state": "available", "completeForPattern": True, "querySHA256": fingerprint(query),
                       "rows": len(rows), "uniqueEntities": len(documents), "scheduleReplacementVerified": False}


# MARK: - Dictionaries

def reference_identifiers(documents):
    """Собирает точные QID значений и qualifiers, исключая уже имеющиеся собственные документы."""
    result = set()
    pending = list(documents.values())
    while pending:
        value = pending.pop()
        if not isinstance(value, dict):
            continue
        if value.get("state") == "value" and value.get("type") == "wikibase-entityid":
            qid = value.get("value", {}).get("id")
            require(valid_wikibase_identifier(qid), "invalid_reference_identifier")
            if valid_qid(qid):
                result.add(qid)
        pending.extend(value.values())
    owned = {document.get("qid") for document in documents.values() if isinstance(document, dict)}
    return sorted(result - owned)


def catalog_reference_documents(records, related):
    """Сохраняет обе проекции одного QID: отдельный фильм может одновременно быть эпизодом сериала."""
    return {**{"record_" + qid: document for qid, document in records.items()},
            **{"related_" + qid: document for qid, document in related.items()}}


def collect_references(client, documents, directory):
    """Продолжает загрузку ru/en меток; отсутствие русского названия остаётся явным пробелом."""
    directory = Path(directory)
    references, pending = {}, []
    for qid in reference_identifiers(documents):
        path = directory / "references" / (qid + ".json")
        if path.exists():
            reference = read_json(path)
            validate_asset(reference)
            require(reference["qid"] == qid and reference["kind"] == "reference", "reference_resume_mismatch")
            references[qid] = reference
        else:
            pending.append(qid)
    for offset in range(0, len(pending), 50):
        selected = pending[offset:offset + 50]
        response = client.action({"action": "wbgetentities", "ids": "|".join(selected),
                                  "props": "info|labels", "languages": "ru|en"})
        entities = response.get("entities", {})
        require(set(entities) == set(selected), "incomplete_dictionary_batch")
        for qid in selected:
            entity = entities[qid]
            require(entity.get("id") == qid and isinstance(entity.get("labels"), dict), "invalid_dictionary_entity")
            reference = {"schemaVersion": 1, "qid": qid, "kind": "reference",
                         "source": entity_source(entity, datetime.now(timezone.utc).isoformat())}
            labels = {language: item["value"] for language, item in entity["labels"].items()
                      if language in {"ru", "en"} and isinstance(item.get("value"), str) and item["value"]}
            if labels:
                reference["titles"] = labels
            validate_asset(reference)
            atomic_json(directory / "references" / (qid + ".json"), reference)
            references[qid] = reference
    return references


# MARK: - Validation

def validate_asset(document):
    """Проверяет обязательное происхождение и границы отдельного публичного документа обогащения."""
    require(isinstance(document, dict) and type(document.get("schemaVersion")) is int and document["schemaVersion"] == 1
            and valid_qid(document.get("qid")), "invalid_asset_identity")
    kind = document.get("kind")
    require(kind in {"synopsis", "reference", "season", "episode"}, "invalid_asset_kind")
    source = document.get("source", {})
    require(type(source.get("revision")) is int and source["revision"] > 0
            and isinstance(source.get("retrievedAt"), str) and bool(source["retrievedAt"]), "invalid_asset_source")
    common_fields = {"schemaVersion", "qid", "kind", "source", "titles"}
    if kind == "synopsis":
        require(set(document) == {"schemaVersion", "qid", "kind", "source", "language", "text", "attribution"},
                "unexpected_synopsis_field")
        require(document["language"] == "ru" and isinstance(document["text"], str)
                and 0 < len(document["text"].encode("utf-8")) <= 128 * 1024, "invalid_synopsis_text")
        attribution = document["attribution"]
        require(isinstance(attribution, dict) and set(attribution) == {"credit", "historyURL", "license",
                    "licenseURL", "modified", "changes", "processingVersion"}
                and attribution.get("license") == "CC-BY-SA-4.0" and attribution.get("modified") is True
                and type(attribution.get("processingVersion")) is int and attribution["processingVersion"] in {1, 2}
                and attribution.get("licenseURL") == "https://creativecommons.org/licenses/by-sa/4.0/"
                and all(isinstance(attribution.get(field), str) and attribution[field]
                        for field in ("credit", "historyURL", "changes")), "missing_text_attribution")
        text_hash = "renderedHTMLSHA256" if attribution["processingVersion"] == 1 else "wikitextSHA256"
        expected_source = {"provider", "pageId", "title", "revision", "url", "articleURL", "retrievedAt", text_hash}
        if attribution["processingVersion"] == 2:
            expected_source.add("format")
            require(source.get("format") == "wikitext", "invalid_text_source_format")
        require(set(source) == expected_source
                and source.get("provider") == "ruwiki" and type(source.get("pageId")) is int and source["pageId"] > 0
                and re.fullmatch(r"[a-f0-9]{64}", source.get(text_hash, ""))
                and source.get("url") == "https://ru.wikipedia.org/w/index.php?oldid=" + str(source["revision"]),
                "invalid_text_revision_url")
        require(attribution["historyURL"] == "https://ru.wikipedia.org/w/index.php?title="
                + quote(source["title"], safe="") + "&action=history"
                and source["articleURL"] == "https://ru.wikipedia.org/wiki/"
                + quote(source["title"].replace(" ", "_"), safe=""), "invalid_text_attribution_url")
    else:
        if kind in {"season", "episode"}:
            common_fields.update({"facts", "works", "relationQuerySHA256"})
        require(set(document) <= common_fields and set(source) == {"qid", "revision", "retrievedAt", "url", "license"}
                and source.get("qid") == document["qid"] and source.get("license") == "CC0-1.0"
                and source.get("url") == "https://www.wikidata.org/wiki/Special:EntityData/"
                    + document["qid"] + ".json?revision=" + str(source["revision"]), "invalid_structured_asset")
        if kind in {"season", "episode"}:
            require(bool(document.get("works")) and all(valid_qid(qid) and state is True
                    for qid, state in document["works"].items()), "invalid_asset_parent")
            require(re.fullmatch(r"[a-f0-9]{64}", document.get("relationQuerySHA256", "")) is not None,
                    "invalid_relation_source")
    if "titles" in document:
        require(isinstance(document["titles"], dict) and bool(document["titles"])
                and set(document["titles"]) <= {"ru", "en"}
                and all(isinstance(title, str) and title for title in document["titles"].values()), "invalid_asset_titles")
    validate_firebase_value(document)
    require(len(canonical_bytes(document)) <= 1024 * 1024, "asset_document_too_large")


# MARK: - Preparation

def prepare_assets(client, records, directory, source_cache):
    """Сохраняет полный результат ограниченной выборки, не объявляя её полным каталогом."""
    directory = Path(directory)
    require(bool(records) and len(records) <= 100 and all(qid == record.get("qid")
            for qid, record in records.items()), "invalid_asset_sample")
    metadata_path = directory / "input.json"
    metadata = {"schemaVersion": 1, "recordsSHA256": fingerprint(records)}
    if metadata_path.exists():
        require(read_json(metadata_path) == metadata, "asset_input_changed")
    else:
        atomic_json(metadata_path, metadata)
    synopses, statuses = collect_synopses(client, records, directory, source_cache)
    related, related_status = collect_related(client, records, directory)
    references = collect_references(client, catalog_reference_documents(records, related), directory)
    result = {"schemaVersion": 1, "inputSHA256": fingerprint(records),
              "collections": {"synopses": synopses, "related": related, "references": references},
              "coverage": {"requestedWorks": len(records), "synopses": len(synopses),
                  "requestedShows": sum(item["kind"] != "film" for item in records.values()),
                  "synopsisStates": statuses, "related": related_status, "references": len(references),
                  "russianReferenceLabels": sum(bool(item.get("titles", {}).get("ru")) for item in references.values()),
                  "licensedPosters": 0, "mediaStatus": "no_verified_posters_not_downloaded"},
              "completeForRequestedSample": not any(item["state"] == "error" for item in statuses.values()),
              "fullScopeVerified": False, "publicationAllowed": False, "resources": client.budget.summary()}
    previous_path = directory / "assets.json"
    if previous_path.exists():
        previous = read_json(previous_path)
        result["initialResources"] = previous.get("initialResources", previous["resources"])
    else:
        result["initialResources"] = result["resources"]
    atomic_json(directory / "assets.json", result)
    return result
