# Written by Freek Zindel, Arno Bakker
# see LICENSE.txt for license information
#
#this is a very limited torrent rss reader. 
#works on some sites, but not on others due to captchas or username/password requirements for downloads.

#usage: make a torrentfeedreader instance and call refresh whenevey you would like to check that feed for new torrents. e.g. every 15 minutes.
#
# Arno, 2007-05-7: We now store the urls visited on disk and don't recontact them for a certain period
#       I've added special support for vuze torrents that have the links to the .torrent in the RSS XML
#       but not as an <link> tag.
#
#       In addition, I've set the reader to be conservative for now, it only looks at .torrent files
#       directly mentioned in the RSS XML, no recursive parsing, that, in case of vuze, visits a lot
#       of sites unnecessarily and uses Java session IDs (";jsessionid") in the URLs, which renders
#       our do-not-visit-if-recently-visited useless.
#

import os
import sys
import traceback
from Tribler.timeouturlopen import urlOpenTimeout
import re
import urlparse
from xml.dom.minidom import parseString
from threading import Thread,RLock,Event
from time import sleep,time
from sha import sha

from BitTornado.bencode import bdecode,bencode
from Tribler.Overlay.MetadataHandler import MetadataHandler
from Tribler.CacheDB.CacheDBHandler import TorrentDBHandler

URLHIST_TIMEOUT = 7*24*3600.0 # Don't revisit links for this time

DEBUG = True


class TorrentFeedThread(Thread):
    
    __single = None
    
    def __init__(self):
        if TorrentFeedThread.__single:
            raise RuntimeError, "TorrentFeedThread is singleton"
        TorrentFeedThread.__single = self
        Thread.__init__(self)
        self.setDaemon(True)

        self.urls = {}
        self.feeds = []
        self.lock = RLock()
        self.done = Event()

    def getInstance(*args, **kw):
        if TorrentFeedThread.__single is None:
            TorrentFeedThread(*args, **kw)
        return TorrentFeedThread.__single
    getInstance = staticmethod(getInstance)
        
    def register(self,utility):
        self.metahandler = MetadataHandler.getInstance()
        self.torrent_db = TorrentDBHandler()
    
        self.utility = utility
        self.intertorrentinterval = self.utility.config.Read("torrentcollectsleep","int")
        
        filename = self.getfilename()
        try:
            f = open(filename,"rb")
            for line in f.readlines():
                for key in ['active','inactive']:
                    if line.startswith(key):
                        url = line[len(key)+1:-2] # remove \r\n
                        if DEBUG:
                            print "subscrip: Add from file URL",url,"EOU"
                        self.addURL(url,dowrite=False,status=key)
            f.close()        
        except:
            traceback.print_exc()
    
        #self.addURL('http://www.vuze.com/syndication/browse/AZHOT/ALL/X/X/26/X/_/_/X/X/feed.xml')
        
    def addURL(self,url,dowrite=True,status="active"):
        self.lock.acquire()
        if url not in self.urls:
            self.urls[url] = status
            if status == "active":
                feed = TorrentFeedReader(url,self.gethistfilename(url))
                self.feeds.append(feed)
            if dowrite:
                self.writefile()
        self.lock.release()

    def writefile(self):
        filename = self.getfilename()
        f = open(filename,"wb")
        for url in self.urls:
            val = self.urls[url]
            f.write(val+' '+url+'\r\n')
        f.close()
        
    def getfilename(self):
        return os.path.join(self.getdir(),"subscriptions.txt")

    def gethistfilename(self,url):
        # TODO: url2pathname or something that gives a readable filename
        h = sha(url).hexdigest()
        return os.path.join(self.getdir(),h+'.txt')
        
    def getdir(self):
        return os.path.join(self.utility.getConfigPath(),"subscriptions")
        
    def getURLs(self):
        return self.urls # doesn't need to be locked
        
    def setURLStatus(self,url,newstatus):
        self.lock.acquire()
        if DEBUG:
            print >>sys.stderr,"subscrip: setURLStatus",url,newstatus
        newtxt = "active"
        if newstatus == False:
            newtxt = "inactive"
        if DEBUG:
            print >>sys.stderr,"subscrip: setURLStatus: newstatus set to",url,newtxt
        if url in self.urls:
            self.urls[url] = newtxt
            self.writefile()
        elif DEBUG:
                print >>sys.stderr,"subscrip: setURLStatus: unknown URL?",url
        self.lock.release()
    
    def deleteURL(self,url):
        self.lock.acquire()
        if url in self.urls:
            del self.urls[url]
            for i in range(len(self.feeds)):
                feed = self.feeds[i]
                if feed.feed_url == url:
                    del self.feeds[i]
                    break
            self.writefile()
        self.lock.release()
        
    def run(self):
        while not self.done.isSet():
            self.lock.acquire()
            cfeeds = self.feeds[:]
            self.lock.release()
            
            for feed in cfeeds:
                rssurl = feed.feed_url
                if DEBUG:
                    print >>sys.stderr,"suscrip: Opening RSS feed",rssurl
                try:
                    pairs = feed.refresh()
                    for title,urlopenobj in pairs:
                        if DEBUG:
                            print >>sys.stderr,"subscrip: Retrieving",`title`,"from",rssurl
                        if urlopenobj is not None:
                            bdata = urlopenobj.read()
                            urlopenobj.close()
    
                            data = bdecode(bdata)
                            torrent_hash = sha(bencode(data['info'])).digest()
                            if not self.torrent_db.hasTorrent(torrent_hash):
                                if DEBUG:
                                    print >>sys.stderr,"subscript: Storing",`title`
                                self.metahandler.save_torrent(torrent_hash,bdata)
                            elif DEBUG:
                                print >>sys.stderr,"subscript: Not storing",`title`,"already have it"
                        # Sleep in between torrent retrievals        
                        sleep(self.intertorrentinterval) 
                except:
                    traceback.print_exc()
                
            # Sleep in between refreshes
            sleep(15*60)


    def shutdown(self):
        if DEBUG:
            print >>sys.stderr,"subscrip: Shutting down subscriptions module"
        self.done.set()
        self.lock.acquire()
        cfeeds = self.feeds[:]
        self.lock.release()
        for feed in cfeeds:
            feed.shutdown()


class TorrentFeedReader:
    def __init__(self,feed_url,histfilename):
        self.feed_url = feed_url
        self.urls_already_seen = URLHistory(histfilename)
        self.href_re = re.compile('href="(.*?)"')
        self.torrent_types = ['application/x-bittorrent','application/x-download']

    def isTorrentType(self,type):
        return type in self.torrent_types

    def refresh(self):
        """Returns a generator for a list of (title,urllib2openedurl_to_torrent)
        pairs for this feed. TorrentFeedReader instances keep a list of
        torrent urls in memory and will yield a torrent only once.
        If the feed points to a torrent url with webserver problems,
        that url will not be retried.
        urllib2openedurl_to_torrent may be None if there is a webserver problem.
        """
        
        # Load history from disk
        if not self.urls_already_seen.readed:
            self.urls_already_seen.read()
            self.urls_already_seen.readed = True
        
        feed_socket = urlOpenTimeout(self.feed_url,timeout=5)
        feed_xml = feed_socket.read()
        
        feed_dom = parseString(feed_xml)

        entries = [(title,link) for title,link in
                   [(item.getElementsByTagName("title")[0].childNodes[0].data,
                     item.getElementsByTagName("link")[0].childNodes[0].data) for
                    item in feed_dom.getElementsByTagName("item")]
                   if link.endswith(".torrent") and not self.urls_already_seen.contains(link)]


        # vuze feeds contain "enclosure" tags that contain the link to the torrent file as an attribute
        # optimize for that
        for item in feed_dom.getElementsByTagName("item"):
            title = item.getElementsByTagName("title")[0].childNodes[0].data
            #print "ENCLOSURE",item.getElementsByTagName("enclosure")
            k = item.getElementsByTagName("enclosure").length
            #print "ENCLOSURE LEN",k
            for i in range(k):
                child = item.getElementsByTagName("enclosure").item(i)
                #print "ENCLOSURE CHILD",child
                if child.hasAttribute("url"):
                    #print "ENCLOSURE CHILD getattrib",link
                    link = child.getAttribute("url")
                    if not self.urls_already_seen.contains(link):
                        entries.append((title,link))


        if DEBUG:
            print >>sys.stderr,"subscrip: Parse of RSS returned",len(entries),"previously unseen torrents"

#        for title,link in entries:
#            print "Link",link,"is in cache?",self.urls_already_seen.contains(link)
#
#        return

        
        for title,link in entries:
            # print title,link
            try:
                self.urls_already_seen.add(link)
                if DEBUG:
                    print >>sys.stderr,"subscrip: Opening",link
                html_or_tor = urlOpenTimeout(link,timeout=5)
                found_torrent = False
                tor_type = html_or_tor.headers.gettype()
                if self.isTorrentType(tor_type):
                    torrent = html_or_tor
                    found_torrent = True
                    if DEBUG:
                        print >>sys.stderr,"subscrip: Yielding",link
                    yield title,torrent
                elif False: # 'html' in tor_type:
                    html = html_or_tor.read()
                    hrefs = [match.group(1) for match in self.href_re.finditer(html)]
                          
                    urls = []
                    for url in hrefs:
                        if not self.urls_already_seen.contains(url):
                            self.urls_already_seen.add(url)
                            urls.append(urlparse.urljoin(link,url))
                    for url in urls:
                        #print url
                        try:
                            if DEBUG:
                                print >>sys.stderr,"subscrip: Opening",url
                            torrent = urlOpenTimeout(url)
                            url_type = torrent.headers.gettype()
                            #print url_type
                            if self.isTorrentType(url_type):
                                #print "torrent found:",url
                                found_torrent = True
                                if DEBUG:
                                    print >>sys.stderr,"subscrip: Yielding",url
                                yield title,torrent
                                break
                            else:
                                #its not a torrent after all, but just some html link
                                pass
                        except:
                            #url didn't open
                            pass
                if not found_torrent:
                    yield title,None
            except:
                traceback.print_exc()
                yield title,None


    def shutdown(self):
        self.urls_already_seen.write()
        

class URLHistory:
    
    def __init__(self,filename):
        self.urls = {}
        self.filename = filename
        self.readed = False
        
    def add(self,url):
        self.urls[url] = time()
                    
    def contains(self,url):
        
        # Poor man's filter
        if url.endswith(".jpg") or url.endswith(".JPG"):
            return True
        
        t = self.urls.get(url,None)
        if t is None:
            return False
        else:
            now = time()
            return not self.timedout(t,now) # no need to delete
    
    def timedout(self,t,now):
        return (t+URLHIST_TIMEOUT) < now
    
    def read(self):
        if DEBUG:
            print >>sys.stderr,"subscrip: Reading cached",self.filename
        try:
            now = time()
            f = open(self.filename,"rb")
            for line in f.readlines():
                line = line[:-2] # remove \r\n
                idx = line.find(' ')
                timestr = line[0:idx]
                url = line[idx+1:]
                t = float(timestr)
                if not self.timedout(t,now):
                    if DEBUG:
                        print >>sys.stderr,"subscrip: Cached url is",url
                    self.urls[url] = t
                elif DEBUG:
                    print >>sys.stderr,"subscrip: Timed out cached url is",t,url
            f.close()        
        except:
            traceback.print_exc()
        
    def write(self):
        try:
            f = open(self.filename,"wb")
            for url,t in self.urls.iteritems():
                line = str(t)+' '+url+'\r\n'
                f.write(line)
            f.close()        
        except:
            traceback.print_exc()

