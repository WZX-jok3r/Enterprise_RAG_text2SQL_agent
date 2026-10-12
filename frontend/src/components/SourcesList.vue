<script setup lang="ts">

defineProps<{
  sources: any[];
}>();


function getTitle(item:any){

  return (
    item.title ||
    item.filename ||
    item.file_name ||
    item.source ||
    "Knowledge Document"
  );

}


function getScore(item:any){

  const score =
    item.score ??
    item.similarity ??
    item.distance;


  if(score === undefined){
    return null;
  }


  if(typeof score === "number"){
    return score.toFixed(3);
  }


  return score;

}

</script>



<template>

<div class="sources">


  <div class="sources-title">

    📚 Knowledge Sources

  </div>



  <div
    class="source-list"
  >


    <div
      v-for="(item,index) in sources"
      :key="index"
      class="source-card"
    >


      <div class="source-icon">
        📄
      </div>



      <div class="source-body">


        <div class="source-name">

          {{ getTitle(item) }}
          <span v-if="item.document_version"> · v{{ item.document_version }}</span>

        </div>



        <div
          v-if="getScore(item)"
          class="score"
        >

          Similarity:
          {{ getScore(item) }}

        </div>



        <div
          v-if="item.content || item.text"
          class="excerpt"
        >

          {{
            item.content ||
            item.text
          }}

        </div>



      </div>



    </div>


  </div>



</div>


</template>



<style scoped>


.sources {

margin-top:18px;

padding-top:15px;

border-top:1px solid var(--border);

}



.sources-title {

font-size:13px;

font-weight:600;

color:var(--muted);

margin-bottom:12px;

}



.source-list {

display:flex;

flex-direction:column;

gap:10px;

}



.source-card {


display:flex;

gap:12px;

padding:12px;

border-radius:14px;

background:
rgba(255,255,255,.04);

border:1px solid var(--border);

transition:.2s;

}



.source-card:hover {

transform:translateY(-2px);

border-color:var(--primary);

}



.source-icon {

font-size:22px;

}



.source-body {

flex:1;

min-width:0;

}



.source-name {

font-size:14px;

font-weight:600;

margin-bottom:5px;

}



.score {

display:inline-block;

font-size:12px;

padding:3px 8px;

border-radius:10px;

background:rgba(37,99,235,.15);

color:#60a5fa;

margin-bottom:8px;

}



.excerpt {


font-size:13px;

line-height:1.5;

color:var(--muted);

display:-webkit-box;

-webkit-line-clamp:3;

-webkit-box-orient:vertical;

overflow:hidden;

}


</style>
