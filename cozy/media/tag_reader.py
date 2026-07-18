import os
from urllib.parse import unquote, urlparse
import re
import shutil
import subprocess

from gi.repository import GLib, Gst, GstPbutils
from mutagen import File
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from cozy.media.chapter import Chapter
from cozy.media.media_file import MediaFile

_MP4CHAPS_LINE_RE = re.compile(
    r'^\s*Chapter\s*#\d+\s*-\s*(\d+):(\d+):(\d+(?:\.\d+)?)\s*-\s*"?(.*?)"?\s*$'
)

class TagReader:
    def __init__(self, uri: str, discoverer_info: GstPbutils.DiscovererInfo):
        if not uri:
            raise ValueError("URI must not be None or empty")

        if not discoverer_info:
            raise ValueError("discoverer_info must not be None")

        self.uri: str = uri
        self.discoverer_info = discoverer_info

        self.tags: Gst.TagList = discoverer_info.get_tags()
        result, tag_format = self.tags.get_string_index("container-format", 0)
        self.tag_format = tag_format.lower() if result else None

        if not self.tags:
            raise ValueError("Failed to retrieve tags from discoverer_info")

    def get_tags(self) -> MediaFile:
        media_file = MediaFile(
            path=unquote(urlparse(self.uri).path),
            book_name=self._get_book_name(),
            author=self._get_author(),
            reader=self._get_reader(),
            disk=self._get_disk(),
            chapters=self._get_chapters(),
            cover=self._get_cover(),
            modified=self._get_modified(),
        )

        return media_file

    def _get_book_name(self):
        success, value = self.tags.get_string_index(Gst.TAG_ALBUM, 0)

        return value.strip() if success else self._get_book_name_fallback()

    def _get_book_name_fallback(self):
        path = os.path.normpath(self.uri)
        directory_path = os.path.dirname(path)
        directory = os.path.basename(directory_path)
        return unquote(directory)

    def _get_author(self):
        mutagen_file = self._get_mutagen_file()

        if self.tag_format == "ogg":
            authors = self._get_string_list(Gst.TAG_ARTIST)
        elif isinstance(mutagen_file, MP4):
            authors = self._get_string_list(Gst.TAG_ARTIST)
        else:
            authors = self_get_string_list(Gst.TAG_COMPOSER)


        if authors and authors[0]:
            return "; ".join(authors)
        else:
            return _("Unknown")

        # authors = (
          #  self._get_string_list(Gst.TAG_ARTIST)
           # if self.tag_format == "ogg"
            #else self._get_string_list(Gst.TAG_COMPOSER)
        #)

    def _get_reader(self):
        mutagen_file = self._get_mutagen_file()

        if self.tag_format == "ogg":
            readers = self._get_string_list(Gst.TAG_PERFORMER)
        elif isinstance(mutagen_file, MP4):
            readers = self._get_string_list(Gst.TAG_COMPOSER)
        else:
            readers = self._get_string_list(Gst.TAG_ARTIST)

        if readers and readers[0]:
            return "; ".join(readers)
        else:
            return _("Unknown")
        #readers = (
          #  self._get_string_list(Gst.TAG_PERFORMER)
          #  if self.tag_format == "ogg"
          #  else self._get_string_list(Gst.TAG_ARTIST)
        #)

    def _get_disk(self):
        success, value = self.tags.get_uint_index(Gst.TAG_ALBUM_VOLUME_NUMBER, 0)

        return value if success else 1

    def _get_track_number(self):
        success, value = self.tags.get_uint_index(Gst.TAG_TRACK_NUMBER, 0)

        return value if success else 0

    def _get_track_name(self):
        success, value = self.tags.get_string_index(Gst.TAG_TITLE, 0)

        return value.strip() if success else self._get_track_name_fallback()

    def _get_track_name_fallback(self):
        filename = os.path.basename(self.uri)
        filename_without_extension = os.path.splitext(filename)[0]
        return unquote(filename_without_extension)

    def _get_mutagen_file(self):
        if not hasattr(self, "_mutagen_file"):
            path = unquote(urlparse(self.uri).path)
            self._mutagen_file = File(path)
        return self._mutagen_file

    def _get_chapters(self):
        mutagen_file = self._get_mutagen_file()

        if isinstance(mutagen_file, MP4):
            return self._get_mp4_chapters(mutagen_file)
        elif isinstance(mutagen_file, MP3):
            return self._get_mp3_chapters(mutagen_file)
        elif self.tag_format == "ogg":
            return self._get_ogg_chapters()
        else:
            return self._get_single_file_chapter()

    def _get_cover(self):
        success, sample = self.tags.get_sample_index(Gst.TAG_IMAGE, 0)

        if not success:
            success, sample = self.tags.get_sample_index(Gst.TAG_PREVIEW_IMAGE, 0)
        if not success:
            return None

        success, mapflags = sample.get_buffer().map(Gst.MapFlags.READ)
        if not success:
            return None

        cover_bytes = GLib.Bytes(mapflags.data).get_data()
        return cover_bytes

    def _get_length_in_seconds(self):
        return self.discoverer_info.get_duration() / Gst.SECOND

    def _get_modified(self):
        path = unquote(urlparse(self.uri).path)
        return int(os.path.getmtime(path))

    def _get_string_list(self, tag: str):
        success, value = self.tags.get_string_index(tag, 0)

        values = []
        for i in range(self.tags.get_tag_size(tag)):
            success, value = self.tags.get_string_index(tag, i)
            if success:
                values.append(value.strip())

        return values

    def _get_single_file_chapter(self):
        chapter = Chapter(
            name=self._get_track_name(),
            position=0,
            length=self._get_length_in_seconds(),
            number=self._get_track_number(),
        )
        return [chapter]

    def _get_mp4_chapters(self, file: MP4) -> list[Chapter]:
        if not file.chapters or len(file.chapters) == 0:
            path = unquote(urlparse(self.uri).path)
            fallback = self._get_mp4_chapters_via_mp4chaps(path)
            return fallback if fallback else self._get_single_file_chapter()

        chapters = []

        for index, chapter in enumerate(file.chapters):
            if index < len(file.chapters) - 1:
                length = file.chapters[index + 1].start - chapter.start
            else:
                length = self._get_length_in_seconds() - chapter.start

            chapters.append(
                Chapter(
                    name=chapter.title or "",
                    position=int(chapter.start * Gst.SECOND),
                    length=length,
                    number=index + 1,
                )
            )

        return chapters

    def _get_mp4_chapters_via_mp4chaps(self, path: str) -> list[Chapter] | None:
        """
            Fallback for MP4/M4B files using the QuickTime text-track chapter format,
            which mutagen's MP4.chapters does not parse (it only reads the
            'moov.udta.chpl' Nero-style atom). mp4chaps (from mp4v2) reads both.
            Returns None if mp4chaps is unavailable or finds nothing, so the caller
            can fall back to a single chapter as before.
        """
        mp4chaps_path = shutil.which("mp4chaps")
        if not mp4chaps_path:
            return None

        try:
            result = subprocess.run(
                [mp4chaps_path, "-l", path],
                capture_output=True,
                text=True,
                timeout=10,
            )

        except (subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0:
            return None

        raw_chapters = []
        for line in result.stdout.splitlines():
            match = _MP4CHAPS_LINE_RE.match(line)
            if not match:
                continue
            hours, minutes, seconds, title = match.groups()
            start_seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            raw_chapters.append((start_seconds, title))
        if not raw_chapters:
            return None

        chapters = []
        for index, (start_seconds, title) in enumerate(raw_chapters):
            if index < len(raw_chapters) - 1:
                length = raw_chapters[index + 1][0] - start_seconds
            else:
                length = self._get_length_in_seconds() - start_seconds

            chapters.append(
                Chapter(
                    name=title,
                    position=int(start_seconds * Gst.SECOND),
                    length=length,
                    number=index + 1,
                )
            )

        return chapters

    def _get_mp3_chapters(self, file: MP3) -> list[Chapter]:
        if not file.tags or not (chaps := file.tags.getall("CHAP")):
            return self._get_single_file_chapter()

        chapters = []
        chaps.sort(key=lambda k: k.start_time)

        for index, chapter in enumerate(chaps):
            if index < len(chaps) - 1:
                length = (chapter.end_time - chapter.start_time) / 1000
            else:
                length = self._get_length_in_seconds() - chapter.start_time / 1000

            sub_frames = chapter.sub_frames.get("TIT2", ())
            title = sub_frames.text[0] if sub_frames else ""

            chapters.append(
                Chapter(
                    name=title,
                    position=int(chapter.start_time * Gst.MSECOND),
                    length=length,
                    number=index + 1,
                )
            )

        return chapters

    def _get_ogg_chapters(self) -> list[Chapter]:
        comment_list: list[str] = self._get_string_list("extended-comment")
        chapter_dict: dict[int, Chapter] = {}
        chapter_list: list[Chapter] = []

        for comment in comment_list:
            if not comment.lower().startswith("chapter"):
                continue

            try:
                tag, value = comment.split("=", 1)
            except ValueError:
                continue

            if len(tag) not in (10, 14) or not tag[7:10].isdecimal():
                continue  # Tag should be in the form CHAPTER + 3 numbers + NAME (for chapter names only)

            try:
                chapter_num = int(tag[7:10], 10) + 1  # get chapter number from 3 chars
            except ValueError:
                continue

            if chapter_num not in chapter_dict:
                chapter_dict[chapter_num] = Chapter(None, None, None, chapter_num)

            if tag.lower().endswith("name"):
                chapter_dict[chapter_num].name = value
            elif len(tag) == 10:
                chapter_dict[chapter_num].position = self._vorbis_timestamp_to_ns(value)

        if not chapter_dict:
            return self._get_single_file_chapter()

        prev_chapter = None
        for _, chapter in sorted(chapter_dict.items()):
            if not chapter.is_valid():
                return self._get_single_file_chapter()

            if prev_chapter:
                prev_chapter.length = (chapter.position - prev_chapter.position) / Gst.SECOND

            chapter_list.append(chapter)
            prev_chapter = chapter

        prev_chapter.length = self._get_length_in_seconds() - prev_chapter.position / Gst.SECOND

        return chapter_list

    @staticmethod
    def _vorbis_timestamp_to_ns(timestamp: str) -> float | None:
        parts = timestamp.split(":", 2)

        try:
            return (int(parts[0], 10) * 3600 + int(parts[1], 10) * 60 + float(parts[2])) * Gst.SECOND
        except ValueError:
            return None
