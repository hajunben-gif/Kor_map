[out:json][timeout:90];
way(around:2000,36.3667897253441,126.836798762428)["highway"]->.roads;
(
  .roads;
  way(around:2000,36.3667897253441,126.836798762428)["waterway"];
  way(around:2000,36.3667897253441,126.836798762428)["natural"="water"];
  relation(bw.roads)["type"="restriction"];
);
out meta geom;
>;
out meta;
