package com.thermetery.boardview

/**
 * Project identity shown on the About page. The display strings live in
 * the shared renderer (assets/viewer.js) and src/app_info.py; the shell
 * only needs the repo URL, to vet links the renderer asks it to open
 * (tests/test_app_info.py keeps all three in sync).
 */
object AppInfo {
    const val REPO_URL = "https://github.com/Thermetery-Technology-LLC/thermetery-boardview"

    /**
     * https URLs at or under REPO_URL only — never an arbitrary site. The
     * path part (before any query) must not contain dot segments or
     * backslashes, which a browser would normalise out of the repo.
     */
    fun isAllowedExternalUrl(url: String): Boolean {
        if (!(url == REPO_URL || url.startsWith("$REPO_URL/") || url.startsWith("$REPO_URL?"))) {
            return false
        }
        val path = url.substringBefore('?').substringBefore('#').lowercase()
        // Percent-encoded dots / backslashes count too: browsers resolve
        // "%2e%2e" as a ".." segment.
        return !path.contains("/.") && !path.contains('\\') && !path.contains('@') &&
            !path.contains("%2e") && !path.contains("%5c")
    }
}
