import asyncio
import math
from datetime import timedelta, datetime
from typing import List, Any, Optional, AsyncGenerator, Tuple
from urllib.parse import quote

import PTT
import httpx
from bs4 import BeautifulSoup

from db.config import settings
from db.models import TorrentStreams, Season, Episode, MediaFusionMetaData
from scrapers.base_scraper import BaseScraper
from utils.network import CircuitBreaker, batch_process_with_circuit_breaker
from utils.parser import convert_size_to_bytes, is_contain_18_plus_keywords
from utils.runtime_const import BT4G_SEARCH_TTL
from utils.validation_helper import is_video_file


class BT4GScraper(BaseScraper):
    MOVIE_SEARCH_QUERY_TEMPLATES = [
        "{title} {year}",  # Title with year
        "{title}",  # Title only
    ]
    SERIES_SEARCH_QUERY_TEMPLATES = [
        "{title} S{season:02d}E{episode:02d}",  # Standard SXXEYY format
        "{title} S{season:02d}",  # Season-only format
        "{title}",  # Title only
    ]
    ITEMS_PER_PAGE = 15  # BT4G shows 15 items per page
    cache_key_prefix = "bt4g"

    def __init__(self):
        super().__init__(cache_key_prefix=self.cache_key_prefix, logger_name=__name__)
        self.semaphore = asyncio.Semaphore(10)
        self.http_client = httpx.AsyncClient(
            proxy=settings.requests_proxy_url,
            timeout=settings.bt4g_search_timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )

    @BaseScraper.cache(ttl=BT4G_SEARCH_TTL)
    @BaseScraper.rate_limit(calls=2, period=timedelta(seconds=1))
    async def _scrape_and_parse(
        self,
        metadata: MediaFusionMetaData,
        catalog_type: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> List[TorrentStreams]:
        results = []
        processed_unique_data = set()

        search_generators = []
        if catalog_type == "movie":
            for query_template in self.MOVIE_SEARCH_QUERY_TEMPLATES:
                search_query = query_template.format(
                    title=metadata.title, year=metadata.year
                )
                search_generators.append(
                    self.scrape_by_query(
                        processed_unique_data,
                        metadata,
                        search_query,
                        catalog_type,
                    )
                )
        else:  # series
            for query_template in self.SERIES_SEARCH_QUERY_TEMPLATES:
                search_query = query_template.format(
                    title=metadata.title,
                    season=season,
                    episode=episode,
                )
                search_generators.append(
                    self.scrape_by_query(
                        processed_unique_data,
                        metadata,
                        search_query,
                        catalog_type,
                        season=season,
                        episode=episode,
                    )
                )

        try:
            async for stream in self.process_streams(
                *search_generators,
                max_process=settings.bt4g_immediate_max_process,
                max_process_time=settings.bt4g_immediate_max_process_time,
            ):
                results.append(stream)
        except Exception as e:
            self.metrics.record_error("stream_processing_error")
            self.logger.error(f"Error processing streams: {e}")

        return results

    @staticmethod
    def _get_search_url(search_query: str, page: int = 1) -> str:
        encoded_query = quote(search_query)
        return (
            f"{settings.bt4g_url}/search"
            f"?q={encoded_query}"
            f"&category=movie"
            f"&p={page}"
        )

    async def parse_first_page(self, html: str) -> Tuple[List[Any], Optional[int]]:
        """Parse first page and return results and total results count"""
        soup = BeautifulSoup(html, "html.parser")

        # Find total results count
        total_results = None
        results_text = soup.find("span", {"class": "badge"})
        if results_text:
            try:
                total_text = results_text.get_text()
                total_results = int(total_text)
            except (ValueError, AttributeError):
                self.logger.warning("Could not parse total results count")

        # Find search results
        results = soup.find_all("div", class_="result-item")
        return results, total_results

    async def scrape_by_query(
        self,
        processed_unique_data: set[str],
        metadata: MediaFusionMetaData,
        search_query: str,
        catalog_type: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> AsyncGenerator[TorrentStreams, None]:
        try:
            # Get first page
            first_page_url = self._get_search_url(search_query, page=1)
            response = await self.make_request(first_page_url)

            # Parse first page
            first_page_results, total_results = await self.parse_first_page(
                response.text
            )
            if not first_page_results:
                return

            # Calculate needed pages based on max_process and items per page
            max_process = settings.bt4g_immediate_max_process
            if total_results:
                total_pages = min(
                    math.ceil(max_process / self.ITEMS_PER_PAGE),
                    math.ceil(total_results / self.ITEMS_PER_PAGE),
                )
            else:
                total_pages = math.ceil(max_process / self.ITEMS_PER_PAGE)

            self.logger.info(
                f"Found {total_results if total_results else 'unknown'} results "
                f"for {search_query} in BT4G site, processing only {total_pages} pages"
            )

            # Process first page results
            async for stream in self.process_page_results(
                first_page_results,
                metadata,
                catalog_type,
                season,
                episode,
                processed_unique_data,
            ):
                yield stream

            # Fetch and process additional pages if needed
            if total_pages > 1:
                tasks = []
                for page in range(2, total_pages + 1):
                    page_url = self._get_search_url(search_query, page)
                    # Fetch all pages concurrently
                    tasks.append(self.make_request(page_url))

                # Process all additional pages
                try:
                    responses = await asyncio.gather(*tasks)
                    for response in responses:
                        soup = BeautifulSoup(response.text, "html.parser")
                        results = soup.find_all("div", class_="result-item")

                        async for stream in self.process_page_results(
                            results,
                            metadata,
                            catalog_type,
                            season,
                            episode,
                            processed_unique_data,
                        ):
                            yield stream

                except Exception as e:
                    self.logger.error(f"Error processing additional pages: {e}")

        except Exception as e:
            self.metrics.record_error("search_error")
            self.logger.exception(f"Error searching BT4G: {e}")

    async def fetch_and_process_page(
        self,
        page_url: str,
        metadata: MediaFusionMetaData,
        catalog_type: str,
        season: Optional[int],
        episode: Optional[int],
        processed_unique_data: set[str],
    ) -> AsyncGenerator[TorrentStreams, None]:
        """Fetch and process a single page of results"""
        try:
            response = await self.make_request(page_url)

            soup = BeautifulSoup(response.text, "html.parser")
            results = soup.find_all("div", class_="result-item")

            async for stream in self.process_page_results(
                results,
                metadata,
                catalog_type,
                season,
                episode,
                processed_unique_data,
            ):
                yield stream

        except Exception as e:
            self.logger.error(f"Error fetching page {page_url}: {e}")

    async def process_page_results(
        self,
        results: List[Any],
        metadata: MediaFusionMetaData,
        catalog_type: str,
        season: Optional[int],
        episode: Optional[int],
        processed_unique_data: set[str],
    ) -> AsyncGenerator[TorrentStreams, None]:
        """Process results from a single page"""
        self.metrics.record_found_items(len(results))

        circuit_breaker = CircuitBreaker(
            failure_threshold=2, recovery_timeout=10, half_open_attempts=3
        )

        async for result in batch_process_with_circuit_breaker(
            self.process_search_result,
            results,
            5,  # batch_size
            3,  # max_concurrent_batches
            circuit_breaker,
            5,  # max_retries
            metadata=metadata,
            catalog_type=catalog_type,
            season=season,
            episode=episode,
            processed_unique_data=processed_unique_data,
        ):
            if result:
                yield result

    async def process_search_result(
        self,
        result: Any,
        metadata: MediaFusionMetaData,
        catalog_type: str,
        processed_unique_data: set[str],
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[TorrentStreams]:
        """Process a single search result"""
        try:
            title_element = result.find("h5")
            if not title_element:
                return None

            torrent_title = title_element.get_text(strip=True)

            if is_contain_18_plus_keywords(torrent_title):
                self.metrics.record_skip("Adult content")
                return None

            parsed_data = PTT.parse_title(torrent_title, True)

            if not self.validate_title_and_year(
                parsed_data,
                metadata,
                catalog_type,
                torrent_title,
            ):
                return None

            info_elements = result.find("p").find_all("span")

            created_date = info_elements[2].get_text()
            # example: 'Creation Time:\xa02024-03-23'
            created_date = datetime.strptime(
                created_date.split(":")[1].strip(), "%Y-%m-%d"
            )
            seeders = int(result.find("b", {"id": "seeders"}).get_text())

            # Drop if seeders are less than 1 and created date is older than 30 days
            if seeders < 1 and created_date < datetime.now() - timedelta(days=30):
                self.metrics.record_skip("Old torrent with no seeders")
                return None

            total_size = info_elements[4].get_text()
            # example: 'Total Size:4.23GB'
            total_size = convert_size_to_bytes(total_size.split(":")[1].strip())

            page_url = title_element.find("a", href=True)["href"]

            if page_url in processed_unique_data:
                self.metrics.record_skip("Duplicate page URL")
                return None

            # Add page URL to the processed set to avoid duplicates scraping the same page
            processed_unique_data.add(page_url)
            response = await self.make_request(settings.bt4g_url + page_url)
            soup = BeautifulSoup(response.text, "html.parser")

            magnet_element = soup.find("a", {"class": "btn-info"})
            if not magnet_element:
                self.metrics.record_skip("Cloudflare protection")
                return None

            magnet_link = magnet_element["href"]
            info_hash = magnet_link.split("btih:")[1].split("&")[0].lower()

            file_info_elements = soup.find_all("div", {"class": "card-body"})[
                -1
            ].find_all("li")
            file_info = []
            seasons = set()
            episodes = set()
            for index, element in enumerate(file_info_elements):
                file_name = element.contents[0].strip()
                file_size = convert_size_to_bytes(
                    element.find("b", {"class": "cpill"}).get_text(strip=True)
                )
                if (
                    not is_video_file(file_name)
                    or file_size < settings.min_scraping_video_size
                ):
                    continue
                file_parsed_data = PTT.parse_title(file_name)
                seasons.update(parsed_data.get("seasons", []))
                episodes.update(parsed_data.get("episodes", []))
                file_info.append(
                    {
                        "filename": file_name,
                        "file_size": file_size,
                        "index": index,
                        "seasons": file_parsed_data.get("seasons"),
                        "episodes": file_parsed_data.get("episodes"),
                    }
                )

            if not file_info:
                self.metrics.record_skip("No valid video files")
                return None

            largest_file = max(file_info, key=lambda x: x["file_size"])

            stream = TorrentStreams(
                id=info_hash,
                meta_id=metadata.id,
                torrent_name=torrent_title,
                filename=largest_file["filename"] if catalog_type == "movie" else None,
                file_index=largest_file["index"] if catalog_type == "movie" else None,
                size=total_size,
                languages=parsed_data["languages"],
                resolution=parsed_data.get("resolution"),
                codec=parsed_data.get("codec"),
                quality=parsed_data.get("quality"),
                audio=parsed_data.get("audio"),
                source="BT4G",
                catalog=["bt4g_streams"],
                seeders=seeders,
                announce_list=[],
            )

            if catalog_type == "series":
                if not parsed_data["seasons"]:
                    parsed_data["seasons"] = list(seasons)
                if not parsed_data["episodes"]:
                    parsed_data["episodes"] = list(episodes)
                if not self._process_series_data(
                    stream, parsed_data, file_info, season, episode
                ):
                    return None

            self.metrics.record_processed_item()
            self.metrics.record_quality(stream.quality)
            self.metrics.record_source(stream.source)

            return stream

        except Exception as e:
            self.metrics.record_error("result_processing_error")
            self.logger.error(f"Error processing search result: {e}")
            return None

    def _process_series_data(
        self,
        stream: TorrentStreams,
        parsed_data: dict,
        file_info: List[dict],
        season: int,
        episode: int,
    ) -> bool:
        """Process series-specific data and validate season/episode information"""
        if not parsed_data.get("seasons"):
            self.metrics.record_skip("Missing season info")
            return False

        if len(parsed_data["seasons"]) != 1:
            self.metrics.record_skip("Multiple seasons")
            return False

        season_number = parsed_data["seasons"][0]
        if season_number != season:
            self.metrics.record_skip("Season mismatch")
            return False

        # Prepare episode data based on detailed file data
        episode_data = [
            Episode(
                episode_number=file["episodes"][0],
                filename=file.get("filename"),
                size=file.get("file_size"),
                file_index=file.get("index"),
            )
            for file in file_info
            if file.get("episodes")
        ]

        if not episode_data:
            self.metrics.record_skip("No valid episodes")
            return False

        stream.season = Season(
            season_number=season_number,
            episodes=episode_data,
        )
        return True

    async def process_streams(
        self,
        *stream_generators: AsyncGenerator[TorrentStreams, None],
        max_process: int = None,
        max_process_time: int = None,
    ) -> AsyncGenerator[TorrentStreams, None]:
        """Process streams from multiple generators with timeout and limits"""
        queue = asyncio.Queue()
        streams_processed = 0
        active_generators = len(stream_generators)
        processed_unique_data = set()

        async def producer(gen: AsyncGenerator, generator_id: int):
            try:
                async for stream_item in gen:
                    await queue.put((stream_item, generator_id))
            except Exception as err:
                self.logger.exception(f"Error in generator {generator_id}: {err}")
            finally:
                await queue.put(("DONE", generator_id))

        async def queue_processor():
            nonlocal active_generators, streams_processed
            while active_generators > 0:
                try:
                    item, gen_id = await queue.get()

                    if item == "DONE":
                        active_generators -= 1
                        continue

                    if (
                        isinstance(item, TorrentStreams)
                        and item.id not in processed_unique_data
                    ):
                        processed_unique_data.add(item.id)
                        streams_processed += 1
                        yield item

                    if max_process and streams_processed >= max_process:
                        self.logger.info(f"Reached max process limit of {max_process}")
                        return

                except Exception as e:
                    self.logger.error(f"Error processing queue item: {e}")
                    continue

        try:
            async with asyncio.timeout(max_process_time):
                async with asyncio.TaskGroup() as tg:
                    [
                        tg.create_task(producer(gen, i))
                        for i, gen in enumerate(stream_generators)
                    ]
                    async for stream in queue_processor():
                        yield stream

        except asyncio.TimeoutError:
            self.logger.warning(
                f"Stream processing timed out after {max_process_time} seconds"
            )
            self.metrics.record_skip("Max process time")
        except Exception as e:
            self.logger.error(f"Error during stream processing: {e}")
            self.metrics.record_error("stream_processing_error")