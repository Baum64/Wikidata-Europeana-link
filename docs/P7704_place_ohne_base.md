# Entwurf: Diskussionsbeitrag für [[Property talk:P7704]]

Zum Einfügen auf https://www.wikidata.org/wiki/Property_talk:P7704 (Wikitext, englisch).
Stand der Prüfung: 2026-11-20. Die Formatter-URLs `/en/$1` und `/fr/$1` sind bereits entfernt.

---

== Drop "/base/" from entity IDs (place, agent, concept, timespan) ==

Europeana no longer uses the <code>/base/</code> segment in its entity identifiers. I'd like to propose
a new preferred format for P7704 that follows the current Europeana form. I'll wait for feedback before changing anything.

=== Current state at Europeana ===
Checked 2026-11-20:
* The Entity API returns IDs without <code>base</code>, e.g. <code>"id": "http://data.europeana.eu/place/1358"</code> for [[Q622|Ludwigsburg]].
* <code>https://data.europeana.eu/place/1358</code> → 303 to <code>https://www.europeana.eu/collections/place/1358</code>.
* <code>https://data.europeana.eu/place/base/1358</code> → only a 301 redirect to <code>…/place/1358</code>.
* <code>https://www.europeana.eu/de/collections/place/base/1358</code> → '''404'''. If you copy a <code>…/base/…</code> value into the website URL, you get an error page.
* The same holds for agents (<code>agent/base/61829</code> → <code>agent/61829</code> → <code>…/collections/person/61829</code>, [[Q206820|Anders Zorn]]), concepts (<code>concept/base/48</code> → <code>concept/48</code>) and timespans (<code>timespan/base/20</code> → <code>timespan/20</code>).
* Organisations are already stored without <code>base</code> (<code>organisation/&lt;n&gt;</code>). The current format constraint flags them as violations, although they are correct.

=== Proposed changes to this property ===

'''1. Format as a regular expression (P1793)'''
* ''Add'', preferred rank: <code>(place|agent|concept|timespan|organisation)\/[1-9]\d*</code>
* <code>((place|agent|concept)\/base|organisation)\/[1-9]\d*</code>: preferred → normal rank now, deprecated after the conversion (step 5).
* <code>(place|agent|concept|organisation)/base/[1-9]\d+</code>: normal → deprecated rank now (it also excludes the valid <code>organisation/&lt;n&gt;</code> values).

'''2. Format constraint (P2302 → Q21502404, mandatory)'''
* Qualifier P1793, currently <code>(place|agent|concept|item|organisation)/base/[1-9][0-9]*</code>
** transition period (both forms valid): <code>((place|agent|concept|timespan)\/(base\/)?|organisation\/)[1-9]\d*</code>
** after the conversion: <code>(place|agent|concept|timespan|organisation)\/[1-9]\d*</code>
* <code>item</code> is dropped: Europeana item IDs belong to [[Property:P727|Europeana ID (P727)]].
* Constraint status (P2316 mandatory constraint) stays.

'''3. URL match pattern (P8966)'''
* <code>^https?:\/\/data\.europeana\.eu\/((place|agent|concept)/base/[1-9]\d+)</code> → replace with <code>^https?:\/\/data\.europeana\.eu\/(place|agent|concept|timespan)\/(?:base\/)?([1-9]\d*)</code> + URL match replacement value (P8967) <code>\1/\2</code>. This accepts URIs with and without <code>base</code> and yields the new form.
* <code>^https?:\/\/(?:www\.)?europeana\.eu\/(?:\w\w)\/(collections\/(?:place|agent|concept|organisation|person)\/\d+)</code> currently yields <code>collections/person/…</code>, which is not a valid value. Replace it with one pattern per type, because the website uses different type names. Each has the qualifier P13337 <code>europeana.eu</code>:
** <code>^https?:\/\/(?:www\.)?europeana\.eu\/(?:[a-z]{2}\/)?collections\/person\/([1-9]\d*)</code> + P8967 <code>agent/\1</code>
** <code>…collections\/place\/([1-9]\d*)</code> + P8967 <code>place/\1</code>
** <code>…collections\/topic\/([1-9]\d*)</code> + P8967 <code>concept/\1</code>
** <code>…collections\/time\/([1-9]\d*)</code> + P8967 <code>timespan/\1</code>
** <code>…collections\/organisation\/([1-9]\d*)</code> + P8967 <code>organisation/\1</code>
* <code>^https?:\/\/data\.europeana\.eu\/organi[sz]ation\/([1-9]\d*)</code>: unchanged.

'''4. Wikidata property example (P1855)'''
* [[Q206820]]: <code>agent/base/61829</code> → <code>agent/61829</code>
* [[Q730008]]: <code>agent/base/73049</code> → <code>agent/73049</code>
* [[Q50030]]: <code>concept/base/221</code> → <code>concept/221</code>
* [[Q174705]]: <code>concept/base/222</code> → <code>concept/222</code>
* ''Add'' [[Q622]]: <code>place/1358</code> (there is no place example yet).
* The organisation examples stay unchanged.

'''Unchanged:''' formatter URL <code>https://data.europeana.eu/$1</code> (works for both forms; with the new form it needs no extra redirect), formatter URI for RDF resource <code>http://data.europeana.eu/$1</code> (yields exactly Europeana's own <code>id</code>), all other constraints.

A direct website formatter URL (<code>https://www.europeana.eu/collections/$1</code>) is not an option. The website uses <code>person</code>, <code>topic</code> and <code>time</code> instead of <code>agent</code>, <code>concept</code> and <code>timespan</code>, so it would only work for places and organisations.

'''5. Existing values'''
Once there is consensus, the existing <code>…/base/…</code> values would be converted by bot: 496 place statements and roughly 158,000 agent statements. Until then both forms are valid. New values from a sameAs-based match of Europeana places and agents with Wikidata (about 1,700 cities and the remaining persons) would be added in the new form, after the format constraint has been widened.

Opinions? In particular: is anyone aware of tools or data consumers that rely on the <code>/base/</code> form? ~~~~
