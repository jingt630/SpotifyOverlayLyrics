Thinking of a personal extension/mod to spotify. I know that there's a whole Spotify API for every spotify user to allow us to create spotify extensions and things.

## Thoughts (main feature)

What I want to create is an extension that extract the lyrics from spotify if existed for the song user is listening to right now.  It will extract two lines of lyrics (let see what is the information spotify gives and consider as one line of lyrics), and overlay only the text, there should be transparent background to the lyrics. There's existing repo in github for overlaying lyrics but as a mini non-transparent window tab. The overlay should always be the priority and always on top of other tab until the user press minimize or close of the overlay. This overlay feature is similar to the 画中画 feature in bilibili.com and many other chinese websites.

This extension shouldn't be like a chrome extension or something that need to be on a website. it should just be locally on a user's computer as a separate application.

## Additional Feature if possible after the main feature is done:
So in spotify u can't see exactly which word of the lyric the singer is singing currently. I want to add this feature after the above feature requirement is done. Each word will be partially bolded as it's being sing like those lyrics in karoake tv. This feature is also very similar to 酷狗音乐, where kugou's lyric overlay actually sync every word with the singer. If tf  the singer is actually singing one lyric for longer than usual, it will take longer to finish bolding the word of the lyrics.

The other feature is there's a countdown of the last measure before the first lyric is sung. So usually in songs there's an intro instrumental part, and when it's close to the singer starting to sing, there's circles that disappear accroding to the beat of the music to help users countdown when to start singing the song.

I don't really have high expectation on the visual of the UI of the extension, can literally be just solid color.


### 出发点：
What I really want to do is similar to 酷狗音乐的浮窗歌词。However I don't have a kugou account and I have spotify prenium so why not and spotify suppoer personal project.

## Steps (for me, the dev):

*each step should be tested before going to next step*

1. let's figure out how to connect to "Spotify for Developer" to use my own personal spotify account first. No need for any GUI or visual yet. Maybe just in the console or easiest way to connect user to spotify for developer and a way to feedback u have connected successfully.

Btw here's some documentation:
- [OpenAPI spec](https://developer.spotify.com/reference/web-api/open-api-schema.yaml)
- [Spotify for Dev website](https://developer.spotify.com/documentation/web-api)

2. If connnected successfully, let's try to see if I can understand what are the allowed API and what info spotify let me get from it. Important things are： which song, does song has lyric available, is lyric in sync on spotify, if sync which lyric line are we on?

3. How should I organize the lyrics so the current line (first line of lyric) and the next line always show in the overlay？？

Create the overlay extension where the background is transparent.

4. Make the overlay surface functionable where user can enable always on top of screen overlay no matter whatever application the user opens. The close and minimize function should also be created.

5. rework on connecting to user's spotify account GUI to create the necessary visual for it.
