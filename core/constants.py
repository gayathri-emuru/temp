DEFAULT_OPENAI_MODEL = "gpt-5.4-nano"
DEFAULT_APIFY_ACTOR_ID = "fantastic-jobs~advanced-linkedin-job-search-api"

DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_FETCH_LIMIT = 2

DEFAULT_TITLES = [
    "Data Scientist",
    "Machine Learning Engineer",
    "ML Engineer",
    "Data Analyst",
    "Data Engineer",
    "Business Intelligence Analyst",
    "Quantitative Analyst",
]

APOLLO_BULK_MATCH_URL = "https://api.apollo.io/api/v1/people/bulk_match"

APOLLO_EXHAUSTED_MARKERS = [
    "insufficient credits",
    "not enough credits",
    "usage limit reached",
    "quota exceeded",
    "monthly usage limit",
    "payment required",
    "credit",
    "credits",
]

COMPANY_SUFFIXES = [
    "inc",
    "incorporated",
    "llc",
    "l.l.c",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "co",
    "company",
    "plc",
    "lp",
    "llp",
]

TITLE_EXACT_REPLACEMENTS = {
    "ml engineer": "machine learning engineer",
    "ml developer": "machine learning developer",
    "ml scientist": "machine learning scientist",
    "sr data scientist": "senior data scientist",
    "jr data scientist": "junior data scientist",
    "bi analyst": "business intelligence analyst",
    "bi developer": "business intelligence developer",
}

TITLE_REGEX_REPLACEMENTS = [
    (r"\bai\s*/\s*ml\b", "artificial intelligence machine learning"),
    (r"\bml\b", "machine learning"),
    (r"\bai\b", "artificial intelligence"),
    (r"\bbi\b", "business intelligence"),
    (r"\bsr\b", "senior"),
    (r"\bjr\b", "junior"),
    (r"\bassoc\b", "associate"),
]

TITLE_DROP_TOKENS = {
    "remote",
    "hybrid",
    "onsite",
    "on",
    "site",
    "contract",
    "temporary",
    "fulltime",
    "parttime",
}

ROMAN_NUMERAL_MAP = {
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
}

STATE_ABBREV_TO_NAME = {
    "al": "alabama",
    "ak": "alaska",
    "az": "arizona",
    "ar": "arkansas",
    "ca": "california",
    "co": "colorado",
    "ct": "connecticut",
    "de": "delaware",
    "dc": "district of columbia",
    "fl": "florida",
    "ga": "georgia",
    "hi": "hawaii",
    "id": "idaho",
    "il": "illinois",
    "in": "indiana",
    "ia": "iowa",
    "ks": "kansas",
    "ky": "kentucky",
    "la": "louisiana",
    "me": "maine",
    "md": "maryland",
    "ma": "massachusetts",
    "mi": "michigan",
    "mn": "minnesota",
    "ms": "mississippi",
    "mo": "missouri",
    "mt": "montana",
    "ne": "nebraska",
    "nv": "nevada",
    "nh": "new hampshire",
    "nj": "new jersey",
    "nm": "new mexico",
    "ny": "new york",
    "nc": "north carolina",
    "nd": "north dakota",
    "oh": "ohio",
    "ok": "oklahoma",
    "or": "oregon",
    "pa": "pennsylvania",
    "ri": "rhode island",
    "sc": "south carolina",
    "sd": "south dakota",
    "tn": "tennessee",
    "tx": "texas",
    "ut": "utah",
    "vt": "vermont",
    "va": "virginia",
    "wa": "washington",
    "wv": "west virginia",
    "wi": "wisconsin",
    "wy": "wyoming",
}

STATE_NAME_TO_ABBREV = {v: k for k, v in STATE_ABBREV_TO_NAME.items()}

STATE_GEO_REGION_IDS = {
    "united states": "103644278",
    "us": "103644278",
    "usa": "103644278",
    "alabama": "102240587",
    "alaska": "100290991",
    "arizona": "106032500",
    "arkansas": "102790221",
    "california": "102095887",
    "colorado": "105763813",
    "connecticut": "106914527",
    "delaware": "105375497",
    "florida": "101318387",
    "georgia": "103950076",
    "hawaii": "105051999",
    "idaho": "102560739",
    "illinois": "101949407",
    "indiana": "103336534",
    "iowa": "103078544",
    "kansas": "104403803",
    "kentucky": "106470801",
    "louisiana": "101822552",
    "maine": "101102875",
    "maryland": "100809221",
    "massachusetts": "101098412",
    "michigan": "103051080",
    "minnesota": "103411167",
    "mississippi": "106899551",
    "missouri": "101486475",
    "montana": "101758306",
    "nebraska": "101197782",
    "nevada": "101690912",
    "new hampshire": "103532695",
    "new jersey": "101651951",
    "new mexico": "105048220",
    "new york": "105080838",
    "north carolina": "103255397",
    "north dakota": "104611396",
    "ohio": "106981407",
    "oklahoma": "101343299",
    "oregon": "101685541",
    "pennsylvania": "102986501",
    "rhode island": "104877241",
    "south carolina": "102687171",
    "south dakota": "100115110",
    "tennessee": "104629187",
    "texas": "102748797",
    "utah": "104102239",
    "vermont": "104453637",
    "virginia": "101630962",
    "washington": "103977389",
    "west virginia": "106420769",
    "wisconsin": "104454774",
    "wyoming": "100658004",
}

LINKEDIN_PEOPLE_KEYWORDS = [
    "data_science_director",
    "data_science_manager",
    "machine_learning_manager",
]

APIFY_EXHAUSTED_MARKERS = [
    "insufficient credits",
    "monthly usage limit",
    "usage limit reached",
    "payment required",
    "quota exceeded",
    "max monthly usage",
    "not enough credits",
    "you have exceeded",
]

SYSTEM_PROMPT = """
You are a strict job filter for a specific candidate. Your job is to read a job description and decide if the candidate should APPLY or REJECT.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CANDIDATE PROFILE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Candidate is an international student with work authorization/EAD and can work in the US without needing sponsorship for this filter
• Education: Bachelor's / Master's degree
• NO PhD
• NO MD
• NO JD
• Role type: Individual contributor only — NOT manager/director/VP/C-suite
• No security clearance
• Open to relocation anywhere in the US
• No professional licenses such as CPA, CFA, PE, Series 7/63/65/66, or active medical/nursing/pharmacy license

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GENERAL FILTERING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• If a filter is not mentioned in the job description, do NOT reject based on that filter
• If you cannot find enough information for a filter, do NOT reject based on that filter
• If nothing clearly triggers a reject condition, default to APPLY
• Reject only if the condition is clearly REQUIRED, not preferred
• If something is only preferred, do NOT reject
• Do not guess
• Be conservative
• Ignore visa sponsorship, work authorization, C2C/1099/no-W2, and student-only internship conditions
• If the job explicitly says F-1 candidates, OPT candidates, or similar visa-status candidates are not eligible to apply, then REJECT
• If the job says "PhD or Master's" or "Requires PhD or Master's", this is ACCEPT for education and you should continue checking the remaining filters
• Salary rule: REJECT only if the MINIMUM salary/package is clearly more than 150,000. Do NOT reject based on the maximum salary. Do NOT reject if salary is missing or unclear.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AUTOMATIC REJECT — if ANY one of these is clearly REQUIRED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EDUCATION REQUIREMENTS:
□ PhD / doctorate is required (not just preferred)
□ MD (medical degree) is required
□ JD (law degree) is required
□ Postdoctoral position (always requires PhD)

CITIZENSHIP / AUTHORIZATION:
□ US citizenship is required
□ Permanent resident required (green card only)
□ The role involves ITAR/export control that restricts to US citizens

SECURITY CLEARANCE:
□ Any security clearance is required

EXPERIENCE:
□ Reject ONLY if the MINIMUM required experience in the job description is 4 or more years
□ Do NOT compare the candidate's exact years to the job
□ This is a threshold-based rule only

SENIORITY:
□ Staff-level role
□ Principal-level role
□ Director or above
□ C-suite
□ Management role that is PRIMARILY managing people
  ALLOW: "lead" in title
  ALLOW: "senior" in title

PROFESSIONAL LICENSES:
□ Series 7/63/65/66 required
□ CPA required
□ CFA required
□ Active medical/nursing/pharmacy license required
□ PE required

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT DECISION LOGIC
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Reject only if a reject condition is clearly required
• Do not reject for preferred qualifications
• Do not reject if something is missing or unclear
• For experience, use only the minimum threshold written in the job description
• Do NOT reject a job just because it says 3+ years
• Do reject a job if it says 4+ years
• Do reject a job if it says minimum 4 years or at least 4 years
• Do reject a job if it says 4-6 years because the minimum is 4
• Reject only if the minimum required experience is clearly 4 years or more

Return only one word:
APPLY
or
REJECT
""".strip()
