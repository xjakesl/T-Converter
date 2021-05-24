import re
import zipfile
from io import BytesIO
from time import strftime, gmtime
from urllib.request import urlopen
from flask import Flask, render_template, request, make_response, abort, redirect, url_for, jsonify, send_from_directory
from flask import send_file
from models import Media, Client, MediaClientAssosciation, db
from datetime import datetime, timedelta
from pytube import YouTube, Playlist
from validators import domain
from validators.url import url as URL
from urllib.parse import urlsplit
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC
from mutagen.easyid3 import EasyID3
from forms import Video
from uuid import uuid4
from pytz import utc
from celery import Celery
import os
import ffmpeg


"""Flask Configuration."""
file_path = os.path.join(os.getcwd(), "database")
download_dir = os.path.join(os.getcwd(), "songs")

app = Flask(__name__)
app.config['song_dir'] = 'songs'
app.config['CELERY_BROKER_URL'] = 'redis://192.168.10.62:6379'
app.config['CELERY_RESULT_BACKEND'] = 'redis://192.168.10.62:6379'
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{file_path}"
app.secret_key = "'saDJHASDFUIQhUHDad5A6D45QWDasdasdas"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


def make_celery(flask):
    """
    Celery Configuration

    :param flask    | Takes Flask object
    :return:        | Returns celery_app
    """
    celery_app = Celery(
        flask.import_name,
        backend=flask.config['CELERY_RESULT_BACKEND'],
        broker=flask.config['CELERY_BROKER_URL']

    )
    celery_app.conf.update(
        task_routes={
            'proj.tasks.add': {'queue': 'dev'}
        }
    )

    class ContextTask(celery_app.Task):
        def __call__(self, *args, **kwargs):
            with flask.app_context():
                return self.run(*args, **kwargs)

    celery_app.Task = ContextTask
    return celery_app


celery = make_celery(app)


"""Create DB and all the tables if they dont exist."""
db.init_app(app)
with app.app_context():
    db.create_all()


@app.route('/', methods=['GET', 'POST'])
def index():
    """
    Index flask route

    Request types:
        - GET   : Render index.html template with form object and errors string.
        - POST  : Validates query and schedules ad celery task for playlist or single video

    :return:        : returns resp variable
    """
    def url_format(url_str: str):
        """
        Formats urls and returns video id and playlist boolean in dictionary.

        :param url_str      | Youtube video or playlist url
        :return:            | dict[id: str, playlist: boolean]
        """
        url = urlsplit(url_str)
        if URL(url_str) and domain(url.netloc) and url.netloc == "www.youtube.com" or url.netloc == "youtube.com" or url.netloc == "youtu.be":
            if url.path == "/watch":
                return dict(id=url.query.strip('v='), playlist=False)
            elif url.path == "/playlist":
                return dict(id=url.query.strip('list='), playlist=True)
            elif url.path != "/watch" and url.path != "/playlist":
                return dict(id=url.path.strip('/'), playlist=False)
        else:
            return dict(id='', playlist=None)

    form = Video()
    errors = ""
    resp = make_response(render_template('index.html', form=form, error=errors))
    uid = request.cookies.get('uid')
    if uid is None:
        uid = uuid4()
        now = datetime.now(tz=utc)
        resp.set_cookie(key='uid', value=str(uid), max_age=None)
        client = Client(session_id=str(uid), time_joined=now)
        db.session.add(client)
        db.session.commit()
    if form.validate_on_submit():
        client = db.session.query(Client).filter(Client.session_id == str(uid)).first()
        video_id = url_format(form.url.data)
        if video_id.get('playlist') is True:
            pl = Playlist(f"https://www.youtube.com/playlist?list={video_id.get('id')}")
            urls = pl.video_urls
            for i, v_url in enumerate(urls):
                if client.medias.filter(Media.yt_id == urlsplit(v_url).query.strip('v=')).first() is None:
                    add.apply_async((v_url, uid), queue='dev')
                    # add(v_url, uid)
            return redirect(url_for('index'))
        elif video_id.get('playlist') is False:
            v_url = f"https://www.youtube.com/watch?v={video_id.get('id')}"
            if client.medias.filter(Media.yt_id == video_id.get('id')).first() is None:
                # add(v_url, uid)
                add.apply_async((v_url, uid), queue='dev')
            return redirect(url_for('index'))
    if len(form.errors) > 0:
        for error in form.errors.get('url'):
            errors += error + " | "
        resp = make_response(render_template('index.html', form=form, error=errors))
    return resp


@app.route('/songs_list')
def song_list():
    """
    Endpoint for loading all songs associated to session

    Request types:
        - GET   : Returns JSON of all the songs associated to session.

    :return:            | returns resp variable
    """
    session = request.cookies.get('uid')
    songs = db.session.query(MediaClientAssosciation).join(Client).join(Media).filter(Client.session_id == session).all()
    result = []
    for song in songs:
        result.append(dict(id=song.media.id,
                           title=song.media.title,
                           thumbnail_url=song.media.thumbnail_url,
                           author=song.media.channel,
                           downloaded=bool(song.media.downloaded),
                           file_name=song.media.file_name,
                           size=sizeof_fmt(int(song.media.size)),
                           yt_id=song.media.yt_id,
                           length=strftime("%H:%M:%S", gmtime(song.media.length))))
    return jsonify(result)


@app.route("/download/<path:song_name>")
def get_song(song_name):
    """
    Endpoint for downloading single song.

    Request types:
        - GET   : Returns file(song_name) as attachment

    :param song_name    | Name of the file to be sent
    :return:            | returns resp variable
    """

    session = request.cookies.get('uid')
    song = db.session.query(MediaClientAssosciation).join(Client).join(Media).filter(Client.session_id == session,
                                                                                     Media.file_name == song_name).first()
    db.session.delete(song)
    db.session.commit()
    return send_from_directory(app.config["song_dir"], filename=song_name, as_attachment=True)


@app.route("/download/all")
def get_app_songs():
    """
    Endpoint for downloading all songs associated to session.

    Request types:
        - GET   : Zips and returns all files associated to this session as attachment.

    :return:            | returns resp variable
    """

    session = request.cookies.get('uid')
    songs = db.session.query(MediaClientAssosciation).join(Client).join(Media).filter(Client.session_id == session).all()
    if len(songs) != 0:
        memory_file = BytesIO()     #: Stores zip file in memory instead of Filesystem
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for song in songs:
                absname = os.path.abspath(os.path.join(download_dir, song.media.file_name))
                arcname = os.path.basename(absname)
                try:
                    zf.write(absname, arcname)
                except FileNotFoundError:
                    pass
        memory_file.seek(0)
        for song in songs:
            db.session.delete(song)
        db.session.commit()
        return send_file(memory_file, attachment_filename="songs.zip", as_attachment=True)
    else:
        abort(404)


@celery.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    """
    This function sets up periodic cleanup task

    :param sender       | No idea what this is.
    :param kwargs:      | No idea what this is.
    """
    sender.add_periodic_task(60.0, cleanup.s(), name='Cleanup', queue='dev')


@celery.task(name='cleanup', run_every=timedelta(minutes=1))
def cleanup():
    """Celery Cleanup task. This runs in celery worker"""

    """Check if any of the Song to Client associations has expired and delete, if they have."""
    tz = utc
    now = datetime.now(tz=tz)
    expiration_point = now - timedelta(hours=2)
    requests = db.session.query(MediaClientAssosciation).all()
    if len(requests) != 0:
        for req in requests:
            if tz.localize(req.request_time) < expiration_point:
                db.session.delete(req)
                db.session.commit()

    """Delete all songs that have expired and have no associations to any client"""
    songs_to_del = db.session.query(Media).all()
    if len(songs_to_del) != 0:
        for song in songs_to_del:
            t = song.clients.all()
            if len(t) == 0 and tz.localize(song.expiration) < now:
                path = os.path.join(download_dir, song.file_name)
                if os.path.exists(path):
                    os.remove(path)
                else:
                    print("File doesnt Exist")
                db.session.delete(song)
                db.session.commit()


@celery.task(name='ytd.db.add', rate_limit='100/m')
def add(v_url: str, uid: str):
    """
    This is another Celery task that handles the downloading and Conversion of songs.

    :param v_url        | Video URL
    :param uid:         | session UID of the requester
    """
    video = YouTube(v_url)
    stream = video.streams.filter(only_audio=True, mime_type='audio/mp4').first()
    if stream is not None:
        media = db.session.query(Media).filter(Media.title == stream.title).first()
        client = db.session.query(Client).filter(Client.session_id == uid).first()
        now = datetime.now(tz=utc)
        if media is None:
            """Add to DB and download files"""
            filename = re.sub(' +', ' ', re.sub("[^0-9a-zA-Z\\ \\- \\(\\)\\[\\]]+", "", video.title))
            new_media = Media(title=video.title, channel=video.author, thumbnail_url=video.thumbnail_url,
                              length=video.length, size=stream.filesize, yt_id=video.video_id, expiration=now + timedelta(hours=1),
                              file_name=filename + '.mp3')
            db.session.add(new_media)
            client.medias.append(MediaClientAssosciation(new_media, now))
            path = stream.download(output_path=download_dir, filename=filename, skip_existing=True, max_retries=2)

            """Convert to MP3."""
            mp3_path = path.replace('.mp4', '.mp3')
            converter = ffmpeg.input(path)
            converter = ffmpeg.output(converter, mp3_path)
            ffmpeg.run(converter, quiet=True, overwrite_output=True)
            os.remove(path)

            """Add metadata."""
            mt = video.metadata
            if len(mt.metadata) != 0:
                mtp = dict(
                    Song=mt.metadata[0].get('Song') if mt.metadata[0].get('Song') is not None else video.title,
                    Artist=mt.metadata[0].get('Artist') if mt.metadata[0].get('Artist') is not None else video.author,
                    Album=mt.metadata[0].get('Album') if mt.metadata[0].get('Album') is not None else "",
                )
            else:
                mtp = dict(Song=video.title, Artist=video.author, Album="")
            meta = EasyID3(mp3_path)
            meta['title'] = mtp.get('Song')
            meta['artist'] = mtp.get('Artist')
            meta['album'] = mtp.get('Album')
            meta.save()

            """Add thumbnail."""
            img = urlopen(video.thumbnail_url)

            audio = MP3(mp3_path, ID3=ID3)

            audio.tags.add(
                APIC(
                    encoding=3,
                    mime='image/jpeg',
                    type=3,
                    desc=u'Cover',
                    data=img.read()
                ))
            audio.save()
            new_media.downloaded = True
            db.session.commit()
        else:
            media.expiration = media.expiration + timedelta(hours=1)
            client.medias.append(MediaClientAssosciation(media, now))
            db.session.commit()
    db.session.commit()


def sizeof_fmt(num, suffix='B'):
    """
    This function handles proper display of file sizes

    :param num      | Takes in Filesize in bytes
    :param suffix:  | Suffix for the filesize
    :return:
    """
    for unit in ['', 'K', 'M', 'G', 'T', 'P', 'E', 'Z']:
        if abs(num) < 1000.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1000.0
    return "%.1f%s%s" % (num, 'Yi', suffix)


if __name__ == '__main__':
    app.run()