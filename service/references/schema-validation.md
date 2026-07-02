# Schema Validation — Required & Recommended Fields by @type

For each schema type, fields are classified as:
- **Required**: Must be present for the schema to be useful. Missing = FAIL (severity: high)
- **Recommended**: Should be present for best results. Missing = WARN (severity: medium)
- **AEO-Critical**: Fields specifically important for AI answer engine extraction. Missing = FAIL for AEO checks

---

## Article / BlogPosting / NewsArticle

**Required:**
- `@type` — "Article", "BlogPosting", or "NewsArticle"
- `headline` — Title of the article (max 110 chars)
- `author` — Must be `{"@type": "Person", "name": "..."}`
- `datePublished` — ISO 8601 format
- `image` — At least one image (prefer ImageObject)
- `publisher` — `{"@type": "Organization", "name": "...", "logo": {...}}`

**Recommended:**
- `dateModified` — ISO 8601, should differ from datePublished if updated
- `description` — Summary of the article
- `@id` — Unique URL with fragment identifier
- `mainEntityOfPage` — URL of the page
- `wordCount` — Integer
- `articleSection` — Category/section name
- `keywords` — Array of relevant keywords
- `inLanguage` — Language code (e.g., "en")

**AEO-Critical:**
- `speakable` — `{"@type": "SpeakableSpecification", "cssSelector": [".summary", ".answer"]}`
- `dateModified` — Freshness signal for AI systems
- `author.sameAs` — Links to author's external profiles
- `author.jobTitle` or `author.description` — Expertise signal

---

## Product

**Required:**
- `@type` — "Product"
- `name` — Product name
- `image` — Product image(s)
- `description` — Product description

**Recommended:**
- `brand` — `{"@type": "Brand", "name": "..."}`
- `offers` — `{"@type": "Offer", "price": "...", "priceCurrency": "...", "availability": "..."}`
- `aggregateRating` — `{"@type": "AggregateRating", "ratingValue": "...", "reviewCount": "..."}`
- `review` — Array of Review objects
- `sku` — Product SKU
- `gtin` / `gtin13` / `gtin8` — Product identifier
- `@id` — Unique URL with fragment
- `category` — Product category

**AEO-Critical:**
- `offers.price` + `offers.priceCurrency` — LLMs cite pricing in comparisons
- `aggregateRating` — Trust + selection signal
- `brand.name` — Entity association

---

## FAQPage

**Required:**
- `@type` — "FAQPage"
- `mainEntity` — Array of Question objects

**Each Question:**
- `@type` — "Question"
- `name` — The question text
- `acceptedAnswer` — `{"@type": "Answer", "text": "..."}`

**Recommended:**
- Minimum 3 Q&A pairs (5+ preferred)
- Questions phrased in natural language (How/What/Why/When)
- Answers are concise (50-200 words each)
- `@id` on each Question with fragment anchor

**AEO-Critical:**
- This entire schema is AEO-critical — FAQPage is the most directly extractable format for LLMs
- Answer text should be self-contained (no "as mentioned above")
- Answers should include specific facts, not vague statements

---

## HowTo

**Required:**
- `@type` — "HowTo"
- `name` — Title of the how-to
- `step` — Array of HowToStep objects

**Each HowToStep:**
- `@type` — "HowToStep"
- `name` — Step title
- `text` — Step description

**Recommended:**
- `totalTime` — ISO 8601 duration (e.g., "PT30M")
- `estimatedCost` — `{"@type": "MonetaryAmount", ...}`
- `tool` — Array of tools/materials needed
- `supply` — Array of supplies needed
- `image` — Per-step images
- `description` — Overall how-to description

**AEO-Critical:**
- `step` array with clear, numbered steps — directly extractable
- `totalTime` — LLMs cite time estimates
- Step names should be action-oriented ("Install the package", not "Step 1")

---

## Organization

**Required:**
- `@type` — "Organization"
- `name` — Organization name
- `url` — Website URL

**Recommended:**
- `logo` — `{"@type": "ImageObject", "url": "...", "width": ..., "height": ...}`
- `description` — One-sentence description of what the organization does
- `sameAs` — Array of social profile URLs [LinkedIn, Twitter, Facebook, GitHub, etc.]
- `foundingDate` — Year founded
- `contactPoint` — `{"@type": "ContactPoint", "contactType": "...", "email": "..."}`
- `address` — `{"@type": "PostalAddress", ...}`
- `@id` — Typically the homepage URL with #organization fragment

**AEO-Critical:**
- `description` — This is what LLMs use to describe the brand
- `sameAs` — Entity disambiguation and trust signal
- `name` — Must be consistent across all pages and schemas

---

## WebSite

**Required:**
- `@type` — "WebSite"
- `name` — Site name
- `url` — Homepage URL

**Recommended:**
- `potentialAction` — SearchAction for sitelinks search box
- `publisher` — Reference to Organization
- `@id` — Homepage URL with #website fragment
- `inLanguage` — Primary language

---

## LocalBusiness

**Required:**
- `@type` — "LocalBusiness" (or specific subtype)
- `name` — Business name
- `address` — `{"@type": "PostalAddress", "streetAddress": "...", "addressLocality": "...", "addressRegion": "...", "postalCode": "...", "addressCountry": "..."}`
- `telephone` — Phone number

**Recommended:**
- `openingHoursSpecification` — Hours of operation
- `geo` — `{"@type": "GeoCoordinates", "latitude": ..., "longitude": ...}`
- `image` — Business photos
- `priceRange` — Price level ("$", "$$", "$$$")
- `aggregateRating` — Reviews
- `sameAs` — Social profiles
- `url` — Website URL
- `areaServed` — Service area

**AEO-Critical:**
- `address` complete and consistent with NAP across web
- `aggregateRating` — Trust signal for local queries
- `openingHoursSpecification` — Directly cited by AI for "is [business] open" queries

---

## BreadcrumbList

**Required:**
- `@type` — "BreadcrumbList"
- `itemListElement` — Array of ListItem objects

**Each ListItem:**
- `@type` — "ListItem"
- `position` — Integer (1-based)
- `name` — Breadcrumb label
- `item` — URL of the breadcrumb page

---

## Person (as author)

**Required:**
- `@type` — "Person"
- `name` — Full name

**Recommended:**
- `url` — Author's profile page on the site
- `sameAs` — Array of external profile URLs (LinkedIn, Twitter, academic profiles)
- `jobTitle` — Professional title
- `description` — Brief bio / expertise summary
- `image` — Author headshot
- `worksFor` — `{"@type": "Organization", "name": "..."}`

**AEO-Critical:**
- `sameAs` — Links to verifiable external profiles (E-E-A-T signal)
- `jobTitle` or `description` — Expertise credential
- `url` — Author page with full bio

---

## ImageObject

**Required:**
- `@type` — "ImageObject"
- `url` — Full image URL (absolute, HTTPS)

**Recommended:**
- `width` — Pixel width
- `height` — Pixel height
- `caption` — Image description
- `contentUrl` — Same as url (some consumers prefer this)

---

## Validation Rules

1. All URLs in schema must be absolute (start with https://)
2. All dates must be ISO 8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)
3. @context must be "https://schema.org" (not http://)
4. Nested objects must have @type declared
5. Image fields should use ImageObject, not bare URL strings
6. sameAs should be an array, even with one entry
7. @id should use the pattern: `{page_url}#{type}` (e.g., "https://example.com/#organization")
