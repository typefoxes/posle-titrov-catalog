# Created by d.kotina on 24.11.2026
"""Адресные замеры сюжетных разделов Wikipedia, лицензий Commons и связей эпизодов."""

import re
from pathlib import Path

from scripts.wikimedia.common import (CatalogError, atomic_json, fingerprint,
                                     read_json, require, valid_qid)
from scripts.wikimedia.contract import known_values

# MARK: - Cached Read-only API

def cached_action(client, directory, host, parameters):
    """Сохраняет публичный API-ответ по полному запросу для воспроизводимого ограниченного замера."""
    path = Path(directory) / (fingerprint({"host": host, "parameters": parameters}) + ".json")
    if path.exists():
        return read_json(path)
    response = client.action(parameters, host=host)
    atomic_json(path, response)
    return response


# MARK: - Wikipedia

def russian_plot_probe(client, records, directory):
    """Проверяет точный QID страницы, фиксированную ревизию и непустой раздел «Сюжет»."""
    host = "ru.wikipedia.org"
    rights = cached_action(client, directory, host,
        {"action": "query", "meta": "siteinfo", "siprop": "rightsinfo"}).get("query", {}).get("rightsinfo", {})
    license_address = rights.get("url", "")
    require(license_address in ("https://creativecommons.org/licenses/by-sa/4.0/",
            "https://creativecommons.org/licenses/by-sa/4.0/deed.ru",
            "//creativecommons.org/licenses/by-sa/4.0/"), "wikipedia_license_requires_review")
    results = {}
    for qid, record in records.items():
        candidate = record.get("wikipediaCandidates", {}).get("ru")
        if not candidate:
            continue
        try:
            query = cached_action(client, directory, host, {"action": "query", "titles": candidate["title"],
                "redirects": 1, "prop": "pageprops"})
            pages = list(query.get("query", {}).get("pages", {}).values())
            require(len(pages) == 1 and pages[0].get("pageprops", {}).get("wikibase_item") == qid,
                    "wikipedia_qid_mismatch")
            page_title = pages[0]["title"]
            parsed = cached_action(client, directory, host,
                {"action": "parse", "page": page_title, "prop": "tocdata|revid"}).get("parse", {})
            section_list = parsed.get("tocdata", {}).get("sections")
            require(type(parsed.get("revid")) is int and isinstance(section_list, list)
                    and parsed.get("pageid") == pages[0].get("pageid"),
                    "wikipedia_partial_sections")
            sections = [section for section in section_list
                        if re.sub(r"<[^>]+>", "", section.get("line", "")).strip().casefold() == "сюжет"]
            result = {"revision": parsed["revid"], "title": page_title,
                      "sourceURL": "https://ru.wikipedia.org/w/index.php?oldid=" + str(parsed["revid"]),
                      "license": "CC-BY-SA-4.0", "licenseURL": license_address,
                      "plotSectionPresent": bool(sections), "copiedIntoCatalog": False}
            if sections:
                section = cached_action(client, directory, host, {"action": "parse", "oldid": parsed["revid"],
                    "section": sections[0]["index"], "prop": "wikitext|revid"}).get("parse", {})
                text = section.get("wikitext", {}).get("*", "")
                require(isinstance(text, str) and section.get("revid") == parsed["revid"], "wikipedia_revision_mismatch")
                # Заголовок без содержимого не доказывает наличие сюжета.
                body = re.sub(r"^\s*=+.*?=+\s*$", "", text, flags=re.MULTILINE).strip()
                result["nonemptyPlotSection"] = bool(body)
                result["sectionUTF8Bytes"] = len(body.encode("utf-8"))
                result["sectionSHA256"] = fingerprint(body)
            results[qid] = result
        except CatalogError as error:
            results[qid] = {"error": str(error), "copiedIntoCatalog": False}
    return {"candidatePages": len(results), "nonemptyPlotSections": sum(
            result.get("nonemptyPlotSection", False) for result in results.values()),
            "results": results, "coverageIsSampleOnly": True,
            "publicationRequiresTextProcessingAndAttribution": True}


# MARK: - Commons

def commons_probe(client, records, directory):
    """Проверяет только явные P18 на Commons; не называет любое изображение постером."""
    results = {}
    for qid, record in records.items():
        files = known_values(record, "imageCandidates")
        for filename in files:
            if not isinstance(filename, str):
                continue
            try:
                response = cached_action(client, directory, "commons.wikimedia.org", {"action": "query",
                    "titles": "File:" + filename, "prop": "imageinfo", "iiprop": "url|extmetadata"})
                pages = list(response.get("query", {}).get("pages", {}).values())
                require(len(pages) == 1 and pages[0].get("imageinfo"), "commons_file_not_confirmed")
                info = pages[0]["imageinfo"][0]
                metadata = info.get("extmetadata", {})
                license_name = metadata.get("LicenseShortName", {}).get("value", "")
                license_url = metadata.get("LicenseUrl", {}).get("value", "")
                author = metadata.get("Artist", {}).get("value", "")
                allowed = (license_name in ("Public domain", "CC0", "CC BY 3.0", "CC BY 4.0",
                                            "CC BY-SA 3.0", "CC BY-SA 4.0")
                           and bool(author) and bool(info.get("descriptionurl")))
                results[qid + ":" + fingerprint(filename)[:16]] = {"qid": qid, "filename": filename,
                    "filePage": info.get("descriptionurl"), "licenseName": license_name,
                    "licenseURL": license_url, "authorMetadataPresent": bool(author),
                    "licenseCandidateAccepted": allowed, "posterRoleVerified": False,
                    "mediaDownloaded": False, "copiedIntoCatalog": False}
            except CatalogError as error:
                results[qid + ":" + fingerprint(filename)[:16]] = {"qid": qid, "error": str(error)}
    return {"candidates": len(results), "acceptedLicenseCandidates": sum(
            result.get("licenseCandidateAccepted", False) for result in results.values()),
            "verifiedPosters": 0, "results": results}


# MARK: - Episode Relations

def episode_relation_probe(client, records, directory):
    """Измеряет прямые P179/P4908 связи, номера и точность дат, не перенося прогресс провайдеров."""
    identifiers = sorted(qid for qid, record in records.items() if record["kind"] != "film")
    require(bool(identifiers) and all(valid_qid(qid) for qid in identifiers), "invalid_series_sample")
    values = " ".join("wd:" + qid for qid in identifiers)
    query = ("SELECT ?show (COUNT(DISTINCT ?episode) AS ?episodes) "
        "(COUNT(DISTINCT ?numbered) AS ?numberedEpisodes) (COUNT(DISTINCT ?dated) AS ?dayDatedEpisodes) "
        '(COUNT(DISTINCT ?seasonLinked) AS ?seasonLinkedEpisodes) WHERE { hint:Query hint:optimizer "None" . '
        "VALUES ?show { " + values + " } ?parent (wdt:P179|wdt:P361)? ?show . "
        "?episode (wdt:P179|wdt:P361|wdt:P4908) ?parent; wdt:P31/wdt:P279* wd:Q21191270 . "
        "OPTIONAL { ?episode (wdt:P1545|p:P179/pq:P1545|p:P361/pq:P1545|p:P4908/pq:P1545) ?ordinal . BIND(?episode AS ?numbered) } "
        "OPTIONAL { ?episode p:P577/psv:P577 ?dateNode . ?dateNode wikibase:timePrecision ?precision . "
        "FILTER(?precision >= 11) BIND(?episode AS ?dated) } "
        "OPTIONAL { ?episode wdt:P4908 ?season . BIND(?episode AS ?seasonLinked) } } GROUP BY ?show")
    path = Path(directory) / "episode-relations.json"
    if path.exists():
        cached = read_json(path)
        response = cached["response"] if cached.get("query") == query else None
    else:
        response = None
    if response is None:
        response = client.sparql(query)
        atomic_json(path, {"query": query, "response": response})
    rows = response.get("results", {}).get("bindings")
    require(isinstance(rows, list), "invalid_episode_relation_result")
    projected = {}
    for row in rows:
        qid = row.get("show", {}).get("value", "").removeprefix("http://www.wikidata.org/entity/")
        require(qid in identifiers, "episode_parent_mismatch")
        projected[qid] = {field: int(row[field]["value"]) for field in
                          ("episodes", "numberedEpisodes", "dayDatedEpisodes", "seasonLinkedEpisodes")}
    return {"sampleSeries": len(identifiers), "seriesWithDirectEpisodeRelations": len(projected),
            "results": projected, "zeroRowsAreNotProofOfNoEpisodes": True,
            "relationshipPattern": "episode P179/P361/P4908 parent, parent is show or P179/P361 season; episode P31/P279* television series episode; direct/qualified ordinal; P577 precision>=day",
            "providerEpisodeNumbersTransferred": False}
