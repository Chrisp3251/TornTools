/* Keep countdowns live without repainting the entire Travel Intelligence page. */
(function(){
  const original=window.tiRefresh;
  if(typeof original!=='function')return;
  window.tiRefresh=function(quiet=false){
    if(quiet){
      if(typeof window.tiTickCountdown==='function')window.tiTickCountdown();
      return Promise.resolve();
    }
    return original(false);
  };
})();