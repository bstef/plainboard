const VIDEO_EXT = /\.(webm|mp4|m4v|mov|ogv|ogg)$/i;

interface FileLike {
  filename: string;
  content_type: string;
}

/** Told by the content type, or by the file name where the uploader's browser sent only a generic type. */
export const isVideoFile = (file: FileLike) =>
  file.content_type.startsWith("video/") || VIDEO_EXT.test(file.filename);
export const isImageFile = (file: FileLike) =>
  file.content_type.startsWith("image/");
/** What the viewer shows: pictures and videos. */
export const isMediaFile = (file: FileLike) =>
  isImageFile(file) || isVideoFile(file);
export const isVideoName = (name: string) =>
  VIDEO_EXT.test(name.split(/[?#]/)[0]);
