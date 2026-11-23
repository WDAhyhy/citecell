# Evaluation manifests (pointer dataset)

This directory holds **pointers and annotations only** — never copies of third-party documents.

- `candidates/` — lists of publicly available documents that could be used for evaluation
  (title, company, date, source URL). Nothing here is a document; it is metadata of public announcements.
- Later: annotation manifests (expected values and locations), each entry carrying the source URL and the
  SHA-256 of the file as it was downloaded, plus a download/verify script.

## Source and terms

`candidates/sse_2026-11.jsonl` lists announcements published on the Shanghai Stock Exchange website.
The site's legal statement (https://www.sse.com.cn/home/legal/) allows browsing and downloading for
non-commercial purposes and forbids using the content for profit or republishing it without permission.
For that reason this project does **not** redistribute the announcements: users download them from the
original URLs themselves, and the hash recorded in the manifest tells them whether they obtained the same file.

Automated downloading of the files is blocked by the site (a JavaScript challenge), and this project does
not try to bypass it. Download the files in a browser instead.

Other exchanges were not used because their usage terms could not be found.

## Pointer list with file hashes: `pointer/cninfo_2026-11.jsonl`

30 announcements published on cninfo.com.cn (2026-09 to 2026-11, one per company, found with the keywords
"重大合同", "中标", "采购合同", "销售合同", "日常经营合同", "框架协议"). Each entry has the source URL, the SHA-256 and
size of the file as it was downloaded, and the search keyword. **No documents are included.**

- **Terms status:** no usage terms could be found for this site (checked 2026-11-23; there is no robots.txt).
  The files were downloaded once, in small number and slowly, to a local directory outside this repository,
  for non-commercial evaluation at the project owner's request. They are not redistributed. Treat the
  terms as unknown; do not mass-download.
- The Shanghai Stock Exchange site was **not** used for downloading: its file server answers automated requests
  with a JavaScript challenge, and this project does not try to get around it.
- Verification: the hash lets a user who has downloaded the same file check that it is identical.
