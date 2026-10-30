import unittest

from bot.url_validation import validate_platform_url


class URLValidationTests(unittest.TestCase):
    def test_youtube_video_urls(self):
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/live/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?t=10",
        ):
            with self.subTest(url=url):
                self.assertIsNone(validate_platform_url(url))

    def test_rejects_youtube_channel_and_bad_id(self):
        self.assertIsNotNone(validate_platform_url("https://www.youtube.com/channel/UC123"))
        self.assertIsNotNone(validate_platform_url("https://www.youtube.com/watch?v=bad"))

    def test_instagram_posts_reels_and_stories(self):
        for url in (
            "https://www.instagram.com/p/ABC_123/",
            "https://instagram.com/reel/ABC_123/",
            "https://instagram.com/reels/ABC_123/",
            "https://instagram.com/stories/user.name/123456789/",
            "https://instagram.com/stories/user.name/",
        ):
            with self.subTest(url=url):
                self.assertIsNone(validate_platform_url(url))

    def test_rejects_instagram_profile(self):
        self.assertIsNotNone(validate_platform_url("https://www.instagram.com/user.name/"))
        self.assertIsNotNone(validate_platform_url("https://www.instagram.com/reels/audio/123/"))

    def test_tiktok_direct_and_short_urls(self):
        for url in (
            "https://www.tiktok.com/@user/video/1234567890123456789",
            "https://www.tiktok.com/@user/video/123",
            "https://www.tiktokv.com/embed/1234567890123456789",
            "https://vm.tiktok.com/ZM123abc/",
        ):
            with self.subTest(url=url):
                self.assertIsNone(validate_platform_url(url))

    def test_rejects_tiktok_profile(self):
        self.assertIsNotNone(validate_platform_url("https://www.tiktok.com/@user"))


if __name__ == "__main__":
    unittest.main()
