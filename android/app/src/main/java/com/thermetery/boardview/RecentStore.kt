package com.thermetery.boardview

import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.util.Log
import androidx.documentfile.provider.DocumentFile
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

/**
 * The "Open recent" list (contract §4 listRecent / openRecent /
 * removeRecent / clearRecent; desktop parity with viewer.py
 * `_remember_board_opened` / `_get_recent` / `_clear_recent_persisted`).
 *
 * Persistence strategy, per entry, in order of preference:
 *
 *  1. A **persisted SAF read grant** on the original document / tree URI
 *     (`takePersistableUriPermission`). The in-app pickers
 *     (ACTION_OPEN_DOCUMENT / ACTION_OPEN_DOCUMENT_TREE) always hand out
 *     persistable grants, so picker-opened boards are reopened from the
 *     source — no extra copy, and edits to the file are picked up.
 *  2. An **app-private copy** under `filesDir/recent/<id>/` when no
 *     persistable grant is available — the usual case for ACTION_VIEW
 *     ("Open with") from another app, whose temporary grant dies with the
 *     task. Copies are bounded by [COPY_BUDGET_BYTES] in total (plus the
 *     [LIMIT]-entry cap); the oldest copy-backed entries are evicted first.
 *
 * Entries are recorded only after a board parsed successfully, so failed
 * or cancelled opens never clutter the list. The list itself lives in
 * `filesDir/recent.json` (name, kind, format, size, opened-at, source URI,
 * copy name) — app-private, and `allowBackup=false` keeps it off backups.
 *
 * Every method is synchronized and does file / provider IO: call from a
 * background thread only (MainActivity uses its `recentIo` executor).
 */
class RecentStore(private val context: Context) {

    companion object {
        private const val TAG = "BoardviewRecent"

        /** viewer.py `_RECENT_LIMIT` is 10 too. */
        const val LIMIT = 10

        /** Total ceiling for app-private copies (boards are 1–50 MB). */
        const val COPY_BUDGET_BYTES = 256L * 1024 * 1024

        const val KIND_FILE = "file"
        const val KIND_FOLDER = "folder"

        private const val LIST_FILE = "recent.json"
        private const val COPIES_DIR = "recent"
    }

    data class Entry(
        val id: String,
        /** Display name (DISPLAY_NAME of the file, or the folder name). */
        val name: String,
        /** [KIND_FILE] or [KIND_FOLDER] (eM-Test .asc set). */
        val kind: String,
        /** Original source URI, or null when only a copy is known. */
        val uri: String?,
        /** We hold a persisted read grant on [uri]. */
        val persisted: Boolean,
        /** Name of the app-private copy under recent/<id>/, or null. */
        val copy: String?,
        /** open_board meta.format ("tvw", "fz", "asc", …). */
        val format: String,
        /** Bytes of the parsed board (folder: sum of members). Dedupe key. */
        val size: Long,
        val openedAt: Long,
    )

    /** Where a board being parsed came from; carried by MainActivity's
     *  ParseAttempt and turned into an [Entry] on a successful parse. */
    data class Origin(
        val kind: String,
        /** Source URI as delivered (picker / ACTION_VIEW / a recent entry's). */
        val uri: Uri?,
        /** Non-null when this is a reopen from the recent list. */
        val recentId: String? = null,
        /** True when the session file was staged from the entry's own copy. */
        val fromCopy: Boolean = false,
    )

    private val resolver: ContentResolver get() = context.contentResolver
    private val listFile get() = File(context.filesDir, LIST_FILE)
    private val copiesRoot get() = File(context.filesDir, COPIES_DIR)

    // ------------------------------------------------------------- reading

    @Synchronized
    fun list(): List<Entry> = load()

    @Synchronized
    fun get(id: String): Entry? = load().firstOrNull { it.id == id }

    /** The entry's app-private copy, if it still exists. */
    fun copyFile(e: Entry): File? =
        e.copy?.let { File(File(copiesRoot, e.id), it) }?.takeIf { it.exists() }

    fun holdsGrant(uri: Uri): Boolean = try {
        resolver.persistedUriPermissions.any { it.uri == uri && it.isReadPermission }
    } catch (t: Throwable) {
        false
    }

    /** Best-effort "can this entry be reopened right now?" for the list UI
     *  (desktop greys out missing files). Does a provider query for
     *  persisted URIs — background thread only. */
    fun isAvailable(e: Entry): Boolean {
        if (e.persisted && e.uri != null) {
            val uri = Uri.parse(e.uri)
            if (holdsGrant(uri)) {
                val doc = try {
                    if (e.kind == KIND_FOLDER) DocumentFile.fromTreeUri(context, uri)
                    else DocumentFile.fromSingleUri(context, uri)
                } catch (t: Throwable) {
                    null
                }
                val ok = try {
                    doc != null && doc.exists()
                } catch (t: Throwable) {
                    false
                }
                if (ok) return true
            }
        }
        return copyFile(e) != null
    }

    /** `{"items":[{id,name,kind,format,openedAt,available}]}` for bv.onRecent. */
    fun toJson(): String {
        val arr = JSONArray()
        for (e in list()) {
            arr.put(
                JSONObject()
                    .put("id", e.id)
                    .put("name", e.name)
                    .put("kind", e.kind)
                    .put("format", e.format)
                    .put("openedAt", e.openedAt)
                    .put("available", isAvailable(e))
            )
        }
        return JSONObject().put("items", arr).toString()
    }

    // ------------------------------------------------------------- writing

    /**
     * Record a successful open. [parsed] is the session copy the board was
     * parsed from (cacheDir/boards/…, a file or an .asc directory); it is
     * copied into recent/<id>/ only when no persisted grant is available.
     * Returns the stored entry, or null when it could not be kept.
     */
    @Synchronized
    fun record(origin: Origin, name: String, parsed: File, format: String): Entry? {
        val entries = load().toMutableList()
        val size = sizeOf(parsed)
        val uriStr = origin.uri?.toString()

        val existing = origin.recentId?.let { id -> entries.firstOrNull { it.id == id } }
            ?: uriStr?.let { u -> entries.firstOrNull { it.uri == u } }
            // Same board reached through a different URI (e.g. the picker's
            // externalstorage id vs. a Downloads msf: id from "Open with"):
            // the rows only show name + format, so two would look identical.
            ?: entries.firstOrNull { it.kind == origin.kind && it.name == name && it.size == size }
        val id = existing?.id ?: UUID.randomUUID().toString().substring(0, 12)

        var uri = uriStr
        var persisted = origin.uri != null && !origin.fromCopy && takeGrant(origin.uri)
        if (!persisted && existing != null && existing.persisted && existing.uri != null &&
            holdsGrant(Uri.parse(existing.uri))
        ) {
            // New route has no lasting grant but the old one still does.
            uri = existing.uri
            persisted = true
        }

        var copy: String? = null
        if (!persisted) {
            val reuse = existing?.let { copyFile(it) }
            if (origin.fromCopy && reuse != null) {
                copy = reuse.name
            } else if (size > COPY_BUDGET_BYTES) {
                Log.i(TAG, "Not keeping $name in recent: $size bytes exceeds the copy budget")
                return existing
            } else {
                copy = makeCopy(id, name, parsed) ?: return existing
            }
        }

        val entry = Entry(
            id = id, name = name, kind = origin.kind, uri = uri, persisted = persisted,
            copy = copy, format = format.ifEmpty { existing?.format ?: "" }, size = size,
            openedAt = System.currentTimeMillis(),
        )
        if (existing != null) {
            entries.remove(existing)
            if (existing.copy != null && copy == null) File(copiesRoot, id).deleteRecursively()
        }
        entries.add(0, entry)
        if (existing != null && existing.persisted && existing.uri != null && existing.uri != entry.uri) {
            releaseIfUnused(existing.uri, entries)
        }

        // Count cap, then copy budget (never evicting the entry just added).
        while (entries.size > LIMIT) drop(entries, entries.last())
        while (copiesBytes(entries) > COPY_BUDGET_BYTES) {
            val victim = entries.drop(1).lastOrNull { it.copy != null } ?: break
            drop(entries, victim)
        }
        save(entries)
        return entry
    }

    @Synchronized
    fun remove(id: String) {
        val entries = load().toMutableList()
        val e = entries.firstOrNull { it.id == id } ?: return
        drop(entries, e)
        save(entries)
    }

    /** "Clear recent": forget every entry, delete every copy and release
     *  every persisted grant (the app takes them for this list only). */
    @Synchronized
    fun clear() {
        try {
            for (p in resolver.persistedUriPermissions) {
                try {
                    resolver.releasePersistableUriPermission(
                        p.uri, Intent.FLAG_GRANT_READ_URI_PERMISSION
                    )
                } catch (t: Throwable) {
                    Log.w(TAG, "release failed for ${p.uri}", t)
                }
            }
        } catch (t: Throwable) {
            Log.w(TAG, "persistedUriPermissions failed", t)
        }
        copiesRoot.deleteRecursively()
        save(emptyList())
    }

    // ------------------------------------------------------------- helpers

    private fun takeGrant(uri: Uri): Boolean {
        if (uri.scheme != "content") return false
        return try {
            resolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
            true
        } catch (t: SecurityException) {
            // ACTION_VIEW senders rarely grant FLAG_GRANT_PERSISTABLE_URI_PERMISSION.
            false
        } catch (t: Throwable) {
            Log.w(TAG, "takePersistableUriPermission failed for $uri", t)
            false
        }
    }

    private fun drop(entries: MutableList<Entry>, e: Entry) {
        entries.remove(e)
        File(copiesRoot, e.id).deleteRecursively()
        if (e.persisted && e.uri != null) releaseIfUnused(e.uri, entries)
    }

    private fun releaseIfUnused(uri: String, remaining: List<Entry>) {
        if (remaining.any { it.persisted && it.uri == uri }) return
        try {
            resolver.releasePersistableUriPermission(
                Uri.parse(uri), Intent.FLAG_GRANT_READ_URI_PERMISSION
            )
        } catch (t: Throwable) {
            // Not held (already revoked) — nothing to release.
        }
    }

    private fun makeCopy(id: String, name: String, parsed: File): String? {
        val dir = File(copiesRoot, id)
        val fileName = name.replace(Regex("[\\\\/:*?\"<>|]"), "_").ifEmpty { "board" }
        return try {
            dir.deleteRecursively()
            dir.mkdirs()
            val dest = File(dir, fileName)
            if (!parsed.copyRecursively(dest, overwrite = true)) throw IllegalStateException("copy failed")
            fileName
        } catch (t: Throwable) {
            Log.w(TAG, "Could not keep a recent copy of $name", t)
            dir.deleteRecursively()
            null
        }
    }

    private fun sizeOf(f: File): Long =
        if (f.isDirectory) f.walkTopDown().filter { it.isFile }.sumOf { it.length() } else f.length()

    private fun copiesBytes(entries: List<Entry>): Long =
        entries.sumOf { e -> copyFile(e)?.let { sizeOf(it) } ?: 0L }

    private fun load(): List<Entry> {
        val text = try {
            listFile.takeIf { it.exists() }?.readText() ?: return emptyList()
        } catch (t: Throwable) {
            return emptyList()
        }
        return try {
            val arr = JSONObject(text).optJSONArray("items") ?: return emptyList()
            (0 until arr.length()).mapNotNull { i ->
                val o = arr.optJSONObject(i) ?: return@mapNotNull null
                val id = o.optString("id").takeIf { it.isNotEmpty() } ?: return@mapNotNull null
                Entry(
                    id = id,
                    name = o.optString("name", "board"),
                    kind = if (o.optString("kind") == KIND_FOLDER) KIND_FOLDER else KIND_FILE,
                    uri = o.optString("uri").takeIf { it.isNotEmpty() },
                    persisted = o.optBoolean("persisted"),
                    copy = o.optString("copy").takeIf { it.isNotEmpty() },
                    format = o.optString("format"),
                    size = o.optLong("size"),
                    openedAt = o.optLong("openedAt"),
                )
            }.take(LIMIT)
        } catch (t: Throwable) {
            Log.w(TAG, "recent.json unreadable; starting empty", t)
            emptyList()
        }
    }

    private fun save(entries: List<Entry>) {
        val arr = JSONArray()
        for (e in entries) {
            arr.put(
                JSONObject()
                    .put("id", e.id)
                    .put("name", e.name)
                    .put("kind", e.kind)
                    .put("uri", e.uri ?: "")
                    .put("persisted", e.persisted)
                    .put("copy", e.copy ?: "")
                    .put("format", e.format)
                    .put("size", e.size)
                    .put("openedAt", e.openedAt)
            )
        }
        val tmp = File(context.filesDir, "$LIST_FILE.tmp")
        try {
            tmp.writeText(JSONObject().put("version", 1).put("items", arr).toString())
            if (!tmp.renameTo(listFile)) {
                listFile.writeText(tmp.readText())
                tmp.delete()
            }
        } catch (t: Throwable) {
            Log.w(TAG, "Could not save recent.json", t)
        }
    }
}
